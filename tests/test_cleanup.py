"""Фоновое обслуживание: протухание заявок и чистка старых метрик.

Без статуса ``EXPIRED`` заявка с прошедшим временем подачи просто переставала
попадать в ленту, но в БД оставалась «новой» навсегда: счётчики расходились с
реальностью, а дубликатор мог схлопнуть свежую заявку со старой мёртвой.
"""

from datetime import timedelta

from sqlalchemy import func, select

from app.db.base import SessionLocal
from app.models import ActionLog, Order, OrderStatus, ParseOutcome, ParseStat
from app.services.cleanup import (
    advance_agreed_orders,
    cleanup_once,
    expire_stale_orders,
    prune_old_stats,
)
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

    assert result == {"expired": 1, "work_started": 0, "auto_completed": 0, "stats_pruned": 1}


async def test_cleanup_on_empty_database_is_quiet(session):
    assert await cleanup_once() == {
        "expired": 0, "work_started": 0, "auto_completed": 0, "stats_pruned": 0,
    }


# --- Заявки «в ближайшее время»: закрываются через 48 часов -------------------


async def _make_asap(session, make_order, *, age_hours: float, **kwargs):
    """Срочная заявка без времени подачи, «опубликованная» age_hours назад."""
    order = await make_order(pickup_at=None, **kwargs)
    async with SessionLocal() as db:
        row = await db.get(Order, order.id)
        row.pickup_asap = True
        row.created_at = now_utc_naive() - timedelta(hours=age_hours)
        await db.commit()
    return order


async def test_asap_order_expires_after_48_hours(session, make_order):
    old = await _make_asap(session, make_order, age_hours=49)
    fresh = await _make_asap(session, make_order, age_hours=47)

    affected = await expire_stale_orders(asap_hours=48)

    assert affected == 1
    assert (await _reload(session, old.id)).status == OrderStatus.EXPIRED
    assert (await _reload(session, fresh.id)).status == OrderStatus.NEW


async def test_asap_limit_comes_from_settings_by_default(session, make_order):
    order = await _make_asap(session, make_order, age_hours=49)

    assert await expire_stale_orders() == 1  # ASAP_EXPIRE_HOURS по умолчанию 48
    assert (await _reload(session, order.id)).status == OrderStatus.EXPIRED


async def test_asap_order_taken_by_driver_is_not_expired(session, make_order):
    order = await _make_asap(session, make_order, age_hours=100, taken_by_token="driver-x")

    assert await expire_stale_orders(asap_hours=48) == 0
    assert (await _reload(session, order.id)).status == OrderStatus.NEW


async def test_order_without_time_and_without_asap_is_untouched(session, make_order):
    """Заявка, у которой время просто не разобрали, этим правилом не закрывается."""
    order = await make_order(pickup_at=None)
    async with SessionLocal() as db:
        row = await db.get(Order, order.id)
        row.created_at = now_utc_naive() - timedelta(hours=100)
        await db.commit()

    assert await expire_stale_orders(asap_hours=48) == 0
    assert (await _reload(session, order.id)).status == OrderStatus.NEW


async def test_expired_asap_order_leaves_the_feed(session, make_order):
    from app.web import queries

    old = await _make_asap(session, make_order, age_hours=60)
    alive = await _make_asap(session, make_order, age_hours=1)

    await expire_stale_orders(asap_hours=48)

    page = await queries.fetch_feed(session, page_size=50)
    assert [o.id for o in page.items] == [alive.id]
    assert old.id not in [o.id for o in page.items]


# --- «Договорились» -> «В работе» -> «Выполнен» -------------------------------


async def _agreed(make_order, *, pickup_hours=None, agreed_hours_ago=0.0, **kwargs):
    """Заказ «Договорились»: время подачи через pickup_hours (может быть
    отрицательным) или без времени; договорились agreed_hours_ago часов назад."""
    pickup = None if pickup_hours is None else now_msk_naive() + timedelta(hours=pickup_hours)
    order = await make_order(pickup_at=pickup, status=OrderStatus.AGREED,
                             taken_by_token="driver-x", **kwargs)
    async with SessionLocal() as db:
        row = await db.get(Order, order.id)
        row.taken_at = now_utc_naive() - timedelta(hours=agreed_hours_ago)
        await db.commit()
    return order


async def test_agreed_order_goes_in_progress_at_deadline(session, make_order):
    later = await _agreed(make_order, pickup_hours=5)
    passed = await _agreed(make_order, pickup_hours=-1)

    started, completed = await advance_agreed_orders()

    assert (started, completed) == (1, 0)
    assert (await _reload(session, passed.id)).status == OrderStatus.IN_PROGRESS
    assert (await _reload(session, later.id)).status == OrderStatus.AGREED


async def test_in_progress_order_completes_72_hours_after_deadline(session, make_order):
    fresh = await _agreed(make_order, pickup_hours=-71)
    old = await _agreed(make_order, pickup_hours=-73)

    started, completed = await advance_agreed_orders()

    assert completed == 1
    assert (await _reload(session, old.id)).status == OrderStatus.COMPLETED
    assert (await _reload(session, fresh.id)).status == OrderStatus.IN_PROGRESS
    assert started == 1  # fresh: «договорились» -> «в работе» в том же проходе


async def test_asap_agreed_order_goes_in_progress_after_3_hours(session, make_order):
    soon = await _agreed(make_order, pickup_hours=None, agreed_hours_ago=2)
    due = await _agreed(make_order, pickup_hours=None, agreed_hours_ago=4)

    await advance_agreed_orders()

    assert (await _reload(session, soon.id)).status == OrderStatus.AGREED
    assert (await _reload(session, due.id)).status == OrderStatus.IN_PROGRESS


async def test_asap_agreed_order_completes_75_hours_after_agreement(session, make_order):
    still = await _agreed(make_order, pickup_hours=None, agreed_hours_ago=74)
    gone = await _agreed(make_order, pickup_hours=None, agreed_hours_ago=76)

    await advance_agreed_orders()

    assert (await _reload(session, still.id)).status == OrderStatus.IN_PROGRESS
    assert (await _reload(session, gone.id)).status == OrderStatus.COMPLETED


async def test_driver_confirmation_wins_over_automation(session, make_order):
    """Выполненные и отменённые заказы фоновая задача не трогает."""
    done = await _agreed(make_order, pickup_hours=-100)
    async with SessionLocal() as db:
        (await db.get(Order, done.id)).status = OrderStatus.CANCELLED
        await db.commit()

    assert await advance_agreed_orders() == (0, 0)
    assert (await _reload(session, done.id)).status == OrderStatus.CANCELLED


async def test_progression_writes_history(session, make_order):
    order = await _agreed(make_order, pickup_hours=-1)

    await advance_agreed_orders()

    actions = (
        await session.execute(select(ActionLog.action).where(ActionLog.order_id == order.id))
    ).scalars().all()
    assert "work_started" in actions
