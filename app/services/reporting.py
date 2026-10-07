"""Сводные отчёты по данным: качество разбора, расходы на LLM, состояние ленты.

Один источник и для админки ``/admin``, и для ``scripts/parse_stats.py`` —
иначе цифры в CLI и на сайте рано или поздно начинают расходиться.

Без этих цифр невозможно ответить на главные вопросы проекта:
действительно ли LLM разбирает заявки, какая доля заказов приходит неполной,
сколько денег уходит в месяц и не молчит ли агент.
"""

from collections import Counter
from datetime import date, datetime, time, timedelta

from sqlalchemy import func, select

from app.city_aliases import canonical_city_name
from app.db.base import SessionLocal
from app.models import (
    ORDER_STATUS_LABELS,
    ActionLog,
    Order,
    OrderStatus,
    ParseOutcome,
    ParseStat,
    PendingMessage,
    PendingStatus,
)
from app.timeutil import MSK, now_utc_naive

#: Сдвиг МСК от UTC: ``created_at`` лежит в UTC, а «день» владельцу нужен по Москве.
MSK_OFFSET = MSK.utcoffset(None)


async def order_status_breakdown() -> dict[str, int]:
    """Сколько заказов в каждом статусе + сколько взято водителями."""
    async with SessionLocal() as session:
        rows = (
            await session.execute(
                select(Order.status, func.count(Order.id)).group_by(Order.status)
            )
        ).all()
        taken = (
            await session.execute(
                select(func.count(Order.id)).where(Order.taken_by_token.is_not(None))
            )
        ).scalar_one()
        problems = (
            await session.execute(
                select(func.count(Order.id)).where(Order.has_problem.is_(True))
            )
        ).scalar_one()

    by_status = {status.value: 0 for status in OrderStatus}
    for status, count in rows:
        key = status.value if isinstance(status, OrderStatus) else str(status)
        by_status[key] = count
    return {
        "by_status": by_status,
        "status_labels": {
            status.value: ORDER_STATUS_LABELS[status] for status in OrderStatus
        },
        "taken": taken,
        "with_problems": problems,
        "total": sum(by_status.values()),
    }


def _msk_day(moment: datetime) -> date:
    """День по МСК для наивного UTC-момента (``created_at``)."""
    return (moment + MSK_OFFSET).date()


async def orders_per_day(days: int = 7) -> dict:
    """Сколько заказов появилось в ленте за каждый из последних ``days`` дней (по МСК).

    Считаются все заявки, кроме скрытых (дубли, мусор, удалённые в Telegram), —
    иначе цифра показывала бы не заказы, а шум. Дни без заказов остаются в ряду
    нулями, чтобы по графику было видно провалы.
    """
    days = max(1, days)
    today = _msk_day(now_utc_naive())
    first = today - timedelta(days=days - 1)
    since = datetime.combine(first, time.min) - MSK_OFFSET  # полночь МСК в наивном UTC
    async with SessionLocal() as session:
        moments = (
            await session.execute(
                select(Order.created_at).where(
                    Order.created_at >= since, Order.status != OrderStatus.CANCELLED
                )
            )
        ).scalars().all()

    counts = Counter(_msk_day(moment) for moment in moments)
    rows = [
        {"day": first + timedelta(days=offset), "count": counts.get(first + timedelta(days=offset), 0)}
        for offset in range(days)
    ]
    total = sum(row["count"] for row in rows)
    peak = max((row["count"] for row in rows), default=0)
    for row in rows:
        row["share"] = (row["count"] / peak) if peak else 0.0
    return {
        "days": days,
        "rows": list(reversed(rows)),  # новые сверху
        "total": total,
        "today": rows[-1]["count"],
        "average": total / days,
        "peak": peak,
    }


async def popular_routes(days: int = 7, limit: int = 10) -> dict:
    """Самые частые направления и города за период.

    Города берутся из нормализованных ключей (``from_city_key``/``to_city_key``),
    поэтому «Мин. Воды», «Минводы» и «Минеральные Воды» — одно направление.
    """
    since = now_utc_naive() - timedelta(days=max(1, days))
    live = (Order.created_at >= since, Order.status != OrderStatus.CANCELLED)
    async with SessionLocal() as session:
        route_rows = (
            await session.execute(
                select(
                    Order.from_city_key,
                    Order.to_city_key,
                    func.count(Order.id),
                    func.avg(Order.client_price),
                    func.avg(Order.distance_km),
                    func.max(Order.from_city),
                    func.max(Order.to_city),
                )
                .where(*live, Order.from_city_key.is_not(None), Order.to_city_key.is_not(None))
                .group_by(Order.from_city_key, Order.to_city_key)
                .order_by(func.count(Order.id).desc())
                .limit(limit)
            )
        ).all()
        city_rows = {}
        for column, label in ((Order.from_city_key, "from"), (Order.to_city_key, "to")):
            city_rows[label] = (
                await session.execute(
                    select(column, func.count(Order.id), func.max(
                        Order.from_city if label == "from" else Order.to_city
                    ))
                    .where(*live, column.is_not(None))
                    .group_by(column)
                    .order_by(func.count(Order.id).desc())
                    .limit(limit)
                )
            ).all()

    def name(raw: str | None) -> str:
        return canonical_city_name(raw) or raw or "?"

    return {
        "days": days,
        "routes": [
            {
                "from": name(from_city),
                "to": name(to_city),
                "count": count,
                "avg_price": int(price) if price else None,
                "avg_km": int(km) if km else None,
            }
            for _fk, _tk, count, price, km, from_city, to_city in route_rows
        ],
        "from_cities": [{"name": name(city), "count": count} for _key, count, city in city_rows["from"]],
        "to_cities": [{"name": name(city), "count": count} for _key, count, city in city_rows["to"]],
    }


#: Почему заявка не попала в ленту или была снята с неё: код действия в журнале → подпись.
HIDDEN_REASONS = {
    "duplicate_skipped": "Дубль остановлен при приёме — в базу не попал",
    "duplicate_cancelled": "Дубль проскочил приём, снят фоновой уборкой позже",
    "duplicate_cancelled_on_edit": "После правки сообщения заявка стала повтором — двойник снят сразу",
    "cancelled_message_deleted": "Диспетчер удалил сообщение в Telegram",
    "cancelled_edited_out": "Диспетчер отредактировал сообщение, заявки в нём не осталось",
    "hidden_by_admin": "Скрыта вручную в админке",
}


async def hidden_breakdown(days: int = 7) -> dict:
    """Сколько заявок скрыто или отсеяно за период и по каким причинам.

    Каждое такое событие пишется в журнал один раз, поэтому счёт по журналу —
    это и есть число случаев. Журнал чистится вместе с давно закрытыми заказами,
    поэтому за очень большие периоды цифры занижены.
    """
    since = now_utc_naive() - timedelta(days=max(1, days))
    async with SessionLocal() as session:
        rows = (
            await session.execute(
                select(ActionLog.action, func.count(ActionLog.id))
                .where(
                    ActionLog.created_at >= since,
                    ActionLog.action.in_([*HIDDEN_REASONS, "order_created"]),
                )
                .group_by(ActionLog.action)
            )
        ).all()
    counts = {action: count for action, count in rows}
    return {
        "days": days,
        "created": counts.get("order_created", 0),
        "reasons": [
            {"label": label, "count": counts.get(action, 0)} for action, label in HIDDEN_REASONS.items()
        ],
    }


async def parse_summary(days: int = 7) -> dict:
    """Качество разбора за период.

    Ключевая метрика — ``incomplete_rate``: доля найденных заявок с неполными
    полями. Если она растёт, значит промпт или модель деградировали, и водители
    видят заявки без цены/времени.
    """
    since = now_utc_naive() - timedelta(days=max(1, days))
    async with SessionLocal() as session:
        rows = (
            await session.execute(
                select(
                    ParseStat.outcome,
                    func.count(ParseStat.id),
                    func.coalesce(func.sum(ParseStat.orders_found), 0),
                    func.coalesce(func.sum(ParseStat.missing_fields), 0),
                    func.coalesce(func.sum(ParseStat.prompt_tokens), 0),
                    func.coalesce(func.sum(ParseStat.completion_tokens), 0),
                    func.coalesce(func.avg(ParseStat.latency_ms), 0),
                )
                .where(ParseStat.created_at >= since)
                .group_by(ParseStat.outcome)
            )
        ).all()

    by_outcome = {outcome.value: 0 for outcome in ParseOutcome}
    orders_found = missing_fields = prompt_tokens = completion_tokens = 0
    latency_weighted = 0.0
    latency_rows = 0

    for outcome, count, found, missing, p_tokens, c_tokens, avg_latency in rows:
        key = outcome.value if isinstance(outcome, ParseOutcome) else str(outcome)
        by_outcome[key] = count
        orders_found += int(found or 0)
        missing_fields += int(missing or 0)
        prompt_tokens += int(p_tokens or 0)
        completion_tokens += int(c_tokens or 0)
        if avg_latency:
            latency_weighted += float(avg_latency) * count
            latency_rows += count

    parsed_total = by_outcome["order"] + by_outcome["not_order"] + by_outcome["duplicate"]
    return {
        "days": days,
        "by_outcome": by_outcome,
        "processed": sum(by_outcome.values()),
        "orders_found": orders_found,
        "missing_fields": missing_fields,
        #: Средняя доля неполных полей на одну найденную заявку (0..1).
        "incomplete_rate": (missing_fields / orders_found) if orders_found else 0.0,
        "llm_calls": by_outcome["order"] + by_outcome["not_order"] + by_outcome["duplicate"],
        "prefiltered": by_outcome["prefiltered"],
        #: Насколько предфильтр сократил число обращений к LLM.
        "prefilter_saving": (
            by_outcome["prefiltered"] / parsed_total
            if (parsed_total + by_outcome["prefiltered"]) else 0.0
        ),
        "errors": by_outcome["error"] + by_outcome["queued"],
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
        "avg_latency_ms": int(latency_weighted / latency_rows) if latency_rows else 0,
    }


async def queue_summary() -> dict[str, int]:
    """Состояние очереди повторного разбора."""
    async with SessionLocal() as session:
        rows = (
            await session.execute(
                select(PendingMessage.status, func.count(PendingMessage.id)).group_by(
                    PendingMessage.status
                )
            )
        ).all()
    result = {status.value: 0 for status in PendingStatus}
    for status, count in rows:
        key = status.value if isinstance(status, PendingStatus) else str(status)
        result[key] = count
    return result
