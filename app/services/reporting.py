"""Сводные отчёты по данным: качество разбора, расходы на LLM, состояние ленты.

Один источник и для админки ``/admin``, и для ``scripts/parse_stats.py`` —
иначе цифры в CLI и на сайте рано или поздно начинают расходиться.

Без этих цифр невозможно ответить на главные вопросы проекта:
действительно ли LLM разбирает заявки, какая доля заказов приходит неполной,
сколько денег уходит в месяц и не молчит ли агент.
"""

from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from sqlalchemy import func, select

from app.city_aliases import canonical_city_name
from app.db.base import SessionLocal
from app.models import (
    ORDER_STATUS_LABELS,
    ActionLog,
    ActorType,
    Driver,
    Order,
    OrderStatus,
    OrderSubscription,
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


# --- Пользователи ------------------------------------------------------------------------


def _msk(moment: datetime | None) -> datetime | None:
    """UTC из базы -> время по Москве для показа в админке."""
    return moment + MSK_OFFSET if moment else None


@dataclass
class UserStats:
    taken: int = 0
    active: int = 0
    completed: int = 0
    earned: float = 0.0
    complaints: int = 0


def _stats_by_token(rows) -> dict[str, UserStats]:
    """Считает по взятым заказам; в Python, а не в SQL: ``case()`` с enum даёт в разных базах разное."""
    result: dict[str, UserStats] = {}
    for token, status, price, has_problem in rows:
        stats = result.setdefault(token, UserStats())
        stats.taken += 1
        if status in (OrderStatus.AGREED, OrderStatus.IN_PROGRESS):
            stats.active += 1
        if status == OrderStatus.COMPLETED:
            stats.completed += 1
            stats.earned += float(price or 0)
        if has_problem:
            stats.complaints += 1
    return result


USER_SORTS = {
    "login": "по последнему входу",
    "registered": "по дате регистрации",
    "taken": "по числу взятых заказов",
    "earned": "по сумме выполненных",
}


async def users_overview(*, q: str = "", sort: str = "login", limit: int = 200) -> dict:
    """Все зарегистрированные пользователи (вошедшие через Telegram) с короткой статистикой."""
    now = now_utc_naive()
    week = now - timedelta(days=7)
    async with SessionLocal() as session:
        drivers = list((await session.execute(select(Driver))).scalars().all())
        rows = (
            await session.execute(
                select(Order.taken_by_token, Order.status, Order.client_price, Order.has_problem).where(
                    Order.taken_by_token.like("tg:%")
                )
            )
        ).all()
        subs: dict[int, list] = {}
        for sub in (await session.execute(select(OrderSubscription))).scalars().all():
            subs.setdefault(sub.telegram_id, []).append(sub)

    stats = _stats_by_token(rows)
    needle = q.strip().lower().lstrip("@")
    users = []
    for driver in drivers:
        name = " ".join(part for part in (driver.first_name, driver.last_name) if part)
        if needle and needle not in f"{name} {driver.username or ''} {driver.telegram_id}".lower():
            continue
        user_subs = subs.get(driver.telegram_id, [])
        users.append(
            {
                "driver": driver,
                "name": name or "—",
                "stats": stats.get(driver.token, UserStats()),
                "filters_total": len(user_subs),
                "filters_active": sum(1 for s in user_subs if s.is_active),
                "filters_error": next((s.error for s in user_subs if s.error), None),
                "registered": _msk(driver.created_at),
                "last_login": _msk(driver.last_login_at),
                "is_new": driver.created_at >= week,
            }
        )

    sorters = {
        "login": lambda u: u["driver"].last_login_at or datetime.min,
        "registered": lambda u: u["driver"].created_at,
        "taken": lambda u: u["stats"].taken,
        "earned": lambda u: u["stats"].earned,
    }
    users.sort(key=sorters.get(sort, sorters["login"]), reverse=True)

    return {
        "users": users[:limit],
        "shown": min(len(users), limit),
        "found": len(users),
        "summary": {
            "total": len(drivers),
            "new_week": sum(1 for d in drivers if d.created_at >= week),
            "active_week": sum(1 for d in drivers if d.last_login_at and d.last_login_at >= week),
            "took_orders": sum(1 for d in drivers if stats.get(d.token, UserStats()).taken),
            "with_notifications": sum(1 for group in subs.values() if any(s.is_active for s in group)),
        },
    }


async def user_detail(telegram_id: int, *, orders_limit: int = 50, actions_limit: int = 30) -> dict | None:
    """Всё о пользователе: профиль, статистика, подписка, его заказы и история действий."""
    async with SessionLocal() as session:
        driver = (
            await session.execute(select(Driver).where(Driver.telegram_id == telegram_id))
        ).scalar_one_or_none()
        if driver is None:
            return None
        token = driver.token
        orders = list(
            (
                await session.execute(
                    select(Order)
                    .where(Order.taken_by_token == token)
                    .order_by(Order.taken_at.desc().nullslast(), Order.id.desc())
                )
            ).scalars().all()
        )
        subs = list(
            (
                await session.execute(
                    select(OrderSubscription)
                    .where(OrderSubscription.telegram_id == telegram_id)
                    .order_by(OrderSubscription.id)
                )
            ).scalars().all()
        )
        actions = []
        if orders:
            actions = list(
                (
                    await session.execute(
                        select(ActionLog)
                        .where(
                            ActionLog.order_id.in_([o.id for o in orders]),
                            ActionLog.actor == ActorType.DRIVER,
                        )
                        .order_by(ActionLog.id.desc())
                        .limit(actions_limit)
                    )
                ).scalars().all()
            )

    stats = _stats_by_token(
        [(token, o.status, o.client_price, o.has_problem) for o in orders]
    ).get(token, UserStats())
    return {
        "driver": driver,
        "name": " ".join(part for part in (driver.first_name, driver.last_name) if part) or "—",
        "stats": stats,
        "subscriptions": subs,
        "registered": _msk(driver.created_at),
        "last_login": _msk(driver.last_login_at),
        "orders": orders[:orders_limit],
        "orders_total": len(orders),
        "taken_at": {o.id: _msk(o.taken_at) for o in orders[:orders_limit]},
        "actions": [(_msk(a.created_at), a.order_id, a.action, a.details or "") for a in actions],
        "active_orders": [o for o in orders if o.status in (OrderStatus.AGREED, OrderStatus.IN_PROGRESS)],
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
