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

from sqlalchemy import and_, delete, or_, select, update

from app.config import settings
from app.db.base import SessionLocal
from app.models import (
    OPEN_STATUSES,
    ActionLog,
    ActorType,
    Order,
    OrderStatus,
    ParseStat,
    SubscriptionNotification,
)
from app.parsing.order_builder import FAR_FUTURE, correct_far_year, has_critical_gaps
from app.services.dedupe import cancel_duplicate_orders
from app.timeutil import now_msk_naive, now_utc_naive

log = logging.getLogger("app.cleanup")

#: Сколько заказов переводим в EXPIRED за один проход (защита от длинной транзакции).
_EXPIRE_BATCH = 500
#: Сколько закрытых заказов удаляем за один проход (хвост добирается следующими).
_PURGE_BATCH = 500


async def expire_stale_orders(
    *,
    now: Optional[object] = None,
    grace_hours: Optional[int] = None,
    asap_hours: Optional[int] = None,
) -> int:
    """Переводит протухшие заявки в ``EXPIRED``.

    Два правила: время подачи прошло (с запасом ``grace_hours`` — диспетчер мог
    опоздать, а водитель — забрать «по факту») и заявка «в ближайшее время»
    провисела дольше ``asap_hours`` с публикации — у неё нет времени подачи,
    поэтому без отдельного срока она копилась бы в ленте вечно.

    Возвращает число затронутых заказов.
    """
    reference = now if now is not None else now_msk_naive()
    hours = settings.expire_grace_hours if grace_hours is None else grace_hours
    cutoff = reference - timedelta(hours=max(0, hours))
    asap_limit = settings.asap_expire_hours if asap_hours is None else asap_hours
    asap_cutoff = now_utc_naive() - timedelta(hours=max(0, asap_limit))

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
        # created_at — UTC, поэтому сравниваем с now_utc_naive(), а не с МСК.
        stale_ids += list(
            (
                await session.execute(
                    select(Order.id)
                    .where(
                        Order.status.in_(OPEN_STATUSES),
                        Order.pickup_asap.is_(True),
                        Order.pickup_at.is_(None),
                        Order.created_at < asap_cutoff,
                        Order.taken_by_token.is_(None),
                    )
                    .order_by(Order.id.asc())
                    .limit(_EXPIRE_BATCH)
                )
            ).scalars().all()
        )
        # Страховка: дата подачи может быть разобрана неверно (год, день/месяц) и уехать в
        # будущее — такая заявка иначе висела бы в ленте до этой самой даты.
        max_days = settings.order_max_live_days
        if max_days and max_days > 0:
            age_cutoff = now_utc_naive() - timedelta(days=max_days)
            stale_ids += list(
                (
                    await session.execute(
                        select(Order.id)
                        .where(
                            Order.status.in_(OPEN_STATUSES),
                            Order.created_at < age_cutoff,
                            Order.taken_by_token.is_(None),
                            Order.id.notin_(stale_ids or [0]),
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
                    details=f"cutoff={cutoff.isoformat()} asap_cutoff={asap_cutoff.isoformat()}",
                )
            )
        await session.commit()

    log.info("Протухших заявок переведено в EXPIRED: %s", len(stale_ids))
    return len(stale_ids)


async def fix_far_future_dates(*, now: Optional[object] = None) -> int:
    """Возвращает на место даты подачи, у которых модель сдвинула год вперёд («28.09.2027»).

    Для живых заявок, подача которых дальше :data:`FAR_FUTURE`: если тот же день в этом году
    рядом с моментом публикации заявки, она получает эту дату и дальше протухает обычным
    порядком. Сравниваем именно с публикацией, а не с «сейчас»: заявка могла висеть днями."""
    reference = now if now is not None else now_msk_naive()
    fixed = 0
    async with SessionLocal() as session:
        orders = (
            await session.execute(
                select(Order).where(
                    Order.status.in_(OPEN_STATUSES),
                    Order.taken_by_token.is_(None),
                    Order.pickup_at > reference + FAR_FUTURE,
                )
            )
        ).scalars().all()
        for order in orders:
            published_msk = order.created_at + timedelta(hours=3)  # created_at — UTC, подача — по МСК
            corrected = correct_far_year(order.pickup_at, now=published_msk)
            if corrected == order.pickup_at:
                continue
            session.add(
                ActionLog(
                    order_id=order.id, actor=ActorType.SYSTEM, action="pickup_year_corrected",
                    details=f"{order.pickup_at:%Y-%m-%d %H:%M} -> {corrected:%Y-%m-%d %H:%M}",
                )
            )
            order.pickup_at = corrected
            fixed += 1
        if fixed:
            await session.commit()
            log.info("Исправлен год подачи у заявок: %s", fixed)
    return fixed


async def resolve_clarifications() -> int:
    """«Уточняется» остаётся только у заказа без города или цены.

    Заказы, получившие плашку по прежнему правилу («пассажиров 1-2», «28000+ платка», нет адреса),
    снова становятся обычными: по ним уже можно договориться с диспетчером. Возвращает число снятых."""
    async with SessionLocal() as session:
        orders = (
            await session.execute(
                select(Order).where(
                    Order.status == OrderStatus.NEEDS_CLARIFICATION, Order.taken_by_token.is_(None)
                )
            )
        ).scalars().all()
        resolved = [order for order in orders if not has_critical_gaps(order)]
        for order in resolved:
            order.status = OrderStatus.NEW
            session.add(
                ActionLog(order_id=order.id, actor=ActorType.SYSTEM, action="clarification_resolved")
            )
        if resolved:
            await session.commit()
            log.info("Снята плашка «Уточняется» у заказов: %s", len(resolved))
    return len(resolved)


async def advance_agreed_orders(
    *,
    now: Optional[object] = None,
    work_start_hours: Optional[int] = None,
    complete_hours: Optional[int] = None,
) -> tuple[int, int]:
    """Двигает заказы водителей по цепочке «Договорились» -> «В работе» ->
    «Выполнен». Возвращает ``(стали «в работе», стали «выполнен»)``.

    Дедлайн заказа — время подачи (``pickup_at``, МСК). У заявки без точного
    времени дедлайна нет: считаем его через ``work_start_hours`` после «Договорился»
    (``taken_at``, UTC). «Выполнен» ставится сам через ``complete_hours`` после
    дедлайна, если водитель не нажал «Выполнил» и не отменил заказ.
    """
    start_hours = settings.asap_work_start_hours if work_start_hours is None else work_start_hours
    finish_hours = settings.auto_complete_hours if complete_hours is None else complete_hours
    now_msk = now if now is not None else now_msk_naive()
    now_utc = now_utc_naive()

    def _deadline_passed(extra_hours: int):
        """Условие «дедлайн + extra_hours уже прошёл»."""
        by_pickup = and_(
            Order.pickup_at.is_not(None),
            Order.pickup_at <= now_msk - timedelta(hours=extra_hours),
        )
        by_agreement = and_(
            Order.pickup_at.is_(None),
            Order.taken_at.is_not(None),
            Order.taken_at <= now_utc - timedelta(hours=start_hours + extra_hours),
        )
        return or_(by_pickup, by_agreement)

    async with SessionLocal() as session:
        async def _move(statuses, condition, target, action: str) -> int:
            ids = list(
                (
                    await session.execute(
                        select(Order.id)
                        .where(
                            Order.status.in_(statuses),
                            Order.taken_by_token.is_not(None),
                            condition,
                        )
                        .order_by(Order.id.asc())
                        .limit(_EXPIRE_BATCH)
                    )
                ).scalars().all()
            )
            if not ids:
                return 0
            await session.execute(
                update(Order)
                .where(Order.id.in_(ids))
                .values(status=target)
                .execution_options(synchronize_session=False)
            )
            for order_id in ids:
                session.add(
                    ActionLog(order_id=order_id, actor=ActorType.SYSTEM, action=action)
                )
            await session.commit()
            return len(ids)

        # Сначала «выполнен» (и из «договорились», и из «в работе»): заказ,
        # у которого дедлайн давно позади, не должен задерживаться в «в работе».
        completed = await _move(
            (OrderStatus.AGREED, OrderStatus.IN_PROGRESS),
            _deadline_passed(finish_hours),
            OrderStatus.COMPLETED,
            "auto_completed",
        )
        started = await _move(
            (OrderStatus.AGREED,),
            _deadline_passed(0),
            OrderStatus.IN_PROGRESS,
            "work_started",
        )

    if started or completed:
        log.info("Заказы водителей: в работу %s, выполнено автоматически %s", started, completed)
    return started, completed


async def purge_old_orders(*, retention_days: Optional[int] = None) -> int:
    """Удаляет насовсем давно закрытые заказы, которые никто не брал.

    Под удаление попадают только ``EXPIRED`` и ``CANCELLED`` без владельца
    (``taken_by_token IS NULL``), у которых последнее изменение старше
    ``retention_days``. Всё, что взял водитель (договорился / в работе /
    выполнен), остаётся навсегда: на этом держится статистика профиля.
    Вместе с заказом удаляется его история действий. Возвращает число удалённых.
    """
    days = settings.closed_orders_retention_days if retention_days is None else retention_days
    if not days or days <= 0:
        return 0
    cutoff = now_utc_naive() - timedelta(days=days)

    async with SessionLocal() as session:
        ids = list(
            (
                await session.execute(
                    select(Order.id)
                    .where(
                        Order.status.in_((OrderStatus.EXPIRED, OrderStatus.CANCELLED)),
                        Order.taken_by_token.is_(None),
                        Order.updated_at < cutoff,
                    )
                    .order_by(Order.id.asc())
                    .limit(_PURGE_BATCH)
                )
            ).scalars().all()
        )
        if not ids:
            return 0
        await session.execute(delete(ActionLog).where(ActionLog.order_id.in_(ids)))
        await session.execute(delete(Order).where(Order.id.in_(ids)))
        await session.commit()

    log.info("Удалено давно закрытых заказов: %s (старше %s дн.)", len(ids), days)
    return len(ids)


async def prune_old_notifications(*, keep_days: int = 7) -> int:
    """Чистит журнал отправленных уведомлений о заказах.

    Заказ рассылается только пока ему меньше 30 минут (см. app.services.subscriptions),
    так что записи старше нескольких дней нужны разве что для разбора жалоб.
    """
    cutoff = now_utc_naive() - timedelta(days=keep_days)
    async with SessionLocal() as session:
        result = await session.execute(
            delete(SubscriptionNotification).where(SubscriptionNotification.sent_at < cutoff)
        )
        await session.commit()
    return result.rowcount or 0


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
    corrected = await fix_far_future_dates()
    clarified = await resolve_clarifications()
    expired = await expire_stale_orders()
    started, completed = await advance_agreed_orders()
    purged = await purge_old_orders()
    notifications = await prune_old_notifications()
    pruned = await prune_old_stats()
    duplicates = await cancel_duplicate_orders()
    return {
        "dates_corrected": corrected,
        "clarifications_resolved": clarified,
        "expired": expired,
        "work_started": started,
        "auto_completed": completed,
        "orders_purged": purged,
        "notifications_pruned": notifications,
        "stats_pruned": pruned,
        "duplicates_cancelled": duplicates,
    }


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
