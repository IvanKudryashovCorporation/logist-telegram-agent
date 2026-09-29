"""Сводные отчёты по данным: качество разбора, расходы на LLM, состояние ленты.

Один источник и для админки ``/admin``, и для ``scripts/parse_stats.py`` —
иначе цифры в CLI и на сайте рано или поздно начинают расходиться.

Без этих цифр невозможно ответить на главные вопросы проекта:
действительно ли LLM разбирает заявки, какая доля заказов приходит неполной,
сколько денег уходит в месяц и не молчит ли агент.
"""

from datetime import timedelta

from sqlalchemy import func, select

from app.db.base import SessionLocal
from app.models import (
    ORDER_STATUS_LABELS,
    Order,
    OrderStatus,
    ParseOutcome,
    ParseStat,
    PendingMessage,
    PendingStatus,
)
from app.timeutil import now_utc_naive


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
