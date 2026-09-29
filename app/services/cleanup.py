"""Обслуживание данных: протухание заявок и чистка старых метрик.

Зачем нужно ``EXPIRED``: раньше заявка с прошедшим временем подачи просто
переставала попадать в ленту (фильтр ``pickup_at >= now``), но в БД оставалась
со статусом NEW навсегда. Из-за этого:

* счётчик в шапке сайта и «сколько всего заказов» расходились с реальностью;
* невозможно отличить «заявку ещё не разобрали» от «рейс давно уехал»;
* дубликатор мог схлопнуть свежую заявку со старой мёртвой.

Отдельный статус делает состояние явным и позволяет показывать водителю
историю, а не молча прятать строки.

Взятые водителем заказы (``taken_by_token IS NOT NULL``) фоновая задача НЕ
трогает: это уже личная история водителя, и переписывать её статусом из
фона — плохая идея.
"""

import asyncio
import logging
from datetime import timedelta
from typing import Optional

from sqlalchemy import delete, select, update

from app.config import settings
from app.db.base import SessionLocal
from app.models import (
    OPEN_STATUSES,
    ActionLog,
    ActorType,
    Order,
    OrderStatus,
    ParseStat,
)
from app.timeutil import now_msk_naive, now_utc_naive

log = logging.getLogger("app.cleanup")

#: Сколько заказов переводим в EXPIRED за один проход (защита от длинной транзакции).
_EXPIRE_BATCH = 500


async def expire_stale_orders(
    *, now: Optional[object] = None, grace_hours: Optional[int] = None
) -> int:
    """Переводит заявки с прошедшим временем подачи в ``EXPIRED``.

    Возвращает число затронутых заказов. ``grace_hours`` — сколько часов после
    времени подачи ещё держим заявку в ленте (диспетчер мог опоздать, а
    водитель — забрать «по факту»).
    """
    reference = now if now is not None else now_msk_naive()
    hours = settings.expire_grace_hours if grace_hours is None else grace_hours
    cutoff = reference - timedelta(hours=max(0, hours))

    async with SessionLocal() as session:
        stale_ids = list(
            (
                await session.execute(
                    select(Order.id)
                    .where(
                        Order.status.in_(OPEN_STATUSES),
                        Order.pickup_at.isnot(None),
                        Order.pickup_at < cutoff,
                        Order.taken_by_token.is_(None),
                    )
                    .order_by(Order.id.asc())
                    .limit(_EXPIRE_BATCH)
                )
            ).scalars().all()
        )
        if not stale_ids:
            return 0

        await session.execute(
            update(Order)
            .where(Order.id.in_(stale_ids))
            .values(status=OrderStatus.EXPIRED)
            .execution_options(synchronize_session=False)
        )
        for order_id in stale_ids:
            session.add(
                ActionLog(
                    order_id=order_id,
                    actor=ActorType.SYSTEM,
                    action="expired_by_time",
                    details=f"cutoff={cutoff.isoformat()}",
                )
            )
        await session.commit()

    log.info("Протухших заявок переведено в EXPIRED: %s", len(stale_ids))
    return len(stale_ids)


async def prune_old_stats(*, retention_days: Optional[int] = None) -> int:
    """Удаляет старые строки ``parse_stats``, чтобы таблица не росла вечно."""
    days = settings.stats_retention_days if retention_days is None else retention_days
    if not days or days <= 0:
        return 0
    cutoff = now_utc_naive() - timedelta(days=days)
    async with SessionLocal() as session:
        result = await session.execute(
            delete(ParseStat).where(ParseStat.created_at < cutoff)
        )
        await session.commit()
        removed = result.rowcount or 0
    if removed:
        log.info("Удалено старых строк parse_stats: %s", removed)
    return removed


async def cleanup_once() -> dict[str, int]:
    """Один проход обслуживания — его же вызывает scripts/cleanup_orders.py."""
    expired = await expire_stale_orders()
    pruned = await prune_old_stats()
    return {"expired": expired, "stats_pruned": pruned}


async def run_cleanup_loop(stop_event: asyncio.Event) -> None:
    """Фоновый цикл: обслуживание раз в ``EXPIRE_POLL_MINUTES`` минут."""
    interval = max(1, settings.expire_poll_minutes) * 60
    log.info(
        "Фоновая очистка запущена: интервал %s мин, протухание через %s ч",
        settings.expire_poll_minutes, settings.expire_grace_hours,
    )
    while not stop_event.is_set():
        try:
            await cleanup_once()
        except Exception:
            log.exception("Ошибка фоновой очистки")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            continue
