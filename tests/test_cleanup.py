"""Фоновое обслуживание: протухание заявок и чистка старых метрик.

Без статуса ``EXPIRED`` заявка с прошедшим временем подачи просто переставала
попадать в ленту, но в БД оставалась «новой» навсегда: счётчики расходились с
реальностью, а дубликатор мог схлопнуть свежую заявку со старой мёртвой.
"""

from datetime import timedelta

from sqlalchemy import func, select

from app.db.base import SessionLocal
from app.models import ActionLog, Order, OrderStatus, ParseOutcome, ParseStat
from app.services.cleanup import cleanup_once, expire_stale_orders, prune_old_stats
from app.timeutil import now_msk_naive, now_utc_naive


async def _reload(session, order_id: int) -> Order:
    """Перечитывает заказ в НОВОЙ сессии (аргумент ``session`` намеренно не используется).

    ``expire_stale_orders`` коммитит в своей сессии, поэтому кэш объектов
    фикстуры показывает состояние «до». Соблазн сделать ``session.expire_all()``
    и перечитать в той же сессии приводит к MissingGreenlet: протухший атрибут
    подгружается лениво, а в async-коде это запрос вне greenlet-контекста.
    """
    async with SessionLocal() as fresh:
        return (await fresh.execute(select(Order).where(Order.id == order_id))).scalar_one()


async def test_stale_free_orders_become_expired(session, make_order):
    old = await make_order(pickup_at=now_msk_naive() - timedelta(hours=10))
    fresh = await make_order(pickup_at=now_msk_naive() + timedelta(hours=10))

    affected = await expire_stale_orders(grace_hours=6)

    assert affected == 1
    assert (await _reload(session, old.id)).status == OrderStatus.EXPIRED
    assert (await _reload(session, fresh.id)).status == OrderStatus.NEW


async def test_grace_period_keeps_recent_orders(session, make_order):
    """Грейс нужен: диспетчер мог опоздать, а водитель — забрать «по факту»."""
    order = await make_order(pickup_at=now_msk_naive() - timedelta(hours=2))

    assert await expire_stale_orders(grace_hours=6) == 0
    assert (await _reload(session, order.id)).status == OrderStatus.NEW

    assert await expire_stale_orders(grace_hours=1) == 1
    assert (await _reload(session, order.id)).status == OrderStatus.EXPIRED


async def test_taken_orders_are_never_expired(session, make_order):
    """Взятый заказ — личная история водителя, фон её переписывать не должен."""
    order = await make_order(
        pickup_at=now_msk_naive() - timedelta(days=3), taken_by_token="driver-token"
    )

    assert await expire_stale_orders(grace_hours=6) == 0
    assert (await _reload(session, order.id)).status == OrderStatus.NEW


async def test_closed_orders_are_not_touched(session, make_order):
    past = now_msk_naive() - timedelta(days=3)
    agreed = await make_order(status=OrderStatus.AGREED, pickup_at=past)
    cancelled = await make_order(status=OrderStatus.CANCELLED, pickup_at=past)

    assert await expire_stale_orders(grace_hours=6) == 0

    assert (await _reload(session, agreed.id)).status == OrderStatus.AGREED
    assert (await _reload(session, cancelled.id)).status == OrderStatus.CANCELLED


async def test_orders_without_pickup_time_are_kept(session, make_order):
    """Заявку без времени подачи прятать нельзя — водитель её ждёт."""
    order = await make_order(pickup_at=None)

    assert await expire_stale_orders(grace_hours=6) == 0
    assert (await _reload(session, order.id)).status == OrderStatus.NEW


async def test_expiration_is_logged(session, make_order):
    """Без записи в истории нельзя понять, кто и почему закрыл заявку."""
    order = await make_order(pickup_at=now_msk_naive() - timedelta(hours=20))

    await expire_stale_orders(grace_hours=6)

    actions = list(
        (
            await session.execute(
                select(ActionLog.action).where(ActionLog.order_id == order.id)
            )
        ).scalars().all()
    )

    assert actions == ["expired_by_time"]


async def test_expired_orders_leave_the_public_feed(session, make_order):
    from app.web import queries

    order = await make_order(pickup_at=now_msk_naive() - timedelta(hours=30))
    await expire_stale_orders(grace_hours=6)

    page = await queries.fetch_feed(session, page=1, page_size=50)
    assert order.id not in {item.id for item in page.items}


async def test_expiration_is_idempotent(session, make_order):
    await make_order(pickup_at=now_msk_naive() - timedelta(hours=30))

    assert await expire_stale_orders(grace_hours=6) == 1
    assert await expire_stale_orders(grace_hours=6) == 0


# --- Чистка метрик -----------------------------------------------------------


async def _add_stat(session, *, days_ago: int) -> int:
    """Строка parse_stats с «возрастом» days_ago. Возвращает id (не объект)."""
    stat = ParseStat(
        outcome=ParseOutcome.ORDER,
        orders_found=1,
        missing_fields=0,
        chat_id=-1001,
        message_id=days_ago,
    )
    session.add(stat)
    await session.flush()
    stat_id = int(stat.id)
    # created_at проставляется сервером, поэтому «состариваем» строку явно.
    stat.created_at = now_utc_naive() - timedelta(days=days_ago)
    session.add(stat)
    await session.commit()
    return stat_id


async def test_old_stats_are_pruned(session):
    old_id = await _add_stat(session, days_ago=40)
    recent_id = await _add_stat(session, days_ago=5)

    removed = await prune_old_stats(retention_days=30)

    async with SessionLocal() as fresh:
        remaining = set((await fresh.execute(select(ParseStat.id))).scalars().all())

    assert removed == 1
    assert old_id not in remaining
    assert recent_id in remaining


async def test_zero_retention_keeps_everything(session):
    await _add_stat(session, days_ago=400)

    assert await prune_old_stats(retention_days=0) == 0
    async with SessionLocal() as fresh:
        total = (await fresh.execute(select(func.count(ParseStat.id)))).scalar_one()
    assert total == 1


async def test_cleanup_once_reports_both_counters(session, make_order):
    await make_order(pickup_at=now_msk_naive() - timedelta(hours=30))
    await _add_stat(session, days_ago=365)

    result = await cleanup_once()

    assert result == {"expired": 1, "stats_pruned": 1}


async def test_cleanup_on_empty_database_is_quiet(session):
    assert await cleanup_once() == {"expired": 0, "stats_pruned": 0}
