"""Напоминание водителю про неоплаченную комиссию через N часов после подачи (вопрос 111)."""

import logging
from datetime import datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select
from telethon import TelegramClient
from telethon.errors import RPCError

from app.config import settings
from app.db.base import SessionLocal
from app.models import Driver, Order, OrderStatus

log = logging.getLogger("agent.reminders")

# order_id -> True, если напоминание уже отправлено (одного процесса достаточно для MVP).
_reminded: set[int] = set()

_CHECK_INTERVAL_MINUTES = 15


def start_commission_reminders(client: TelegramClient) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        _check_due_orders,
        "interval",
        minutes=_CHECK_INTERVAL_MINUTES,
        args=[client],
        next_run_time=datetime.now() + timedelta(seconds=30),
    )
    scheduler.start()
    log.info("Планировщик напоминаний о комиссии запущен (каждые %s мин).", _CHECK_INTERVAL_MINUTES)
    return scheduler


async def _check_due_orders(client: TelegramClient) -> None:
    # pickup_at хранится как локальное время (как его вводит диспетчер и видит логист
    # в веб-панели), поэтому сравниваем с локальным now(), а не UTC — иначе на UTC+3
    # напоминание уходит на 3 часа позже, чем нужно.
    deadline = datetime.now() - timedelta(hours=settings.commission_reminder_hours)
    async with SessionLocal() as session:
        orders = (
            await session.execute(
                select(Order).where(
                    Order.status == OrderStatus.IN_PROGRESS,
                    Order.commission_paid.is_(False),
                    Order.pickup_at.is_not(None),
                    Order.pickup_at <= deadline,
                )
            )
        ).scalars().all()

        for order in orders:
            if order.id in _reminded or order.assigned_driver_id is None:
                continue
            driver = await session.get(Driver, order.assigned_driver_id)
            if driver is None:
                continue
            try:
                await client.send_message(
                    driver.tg_user_id,
                    f"Напоминаю про комиссию по заказу #{order.id} — прошло больше "
                    f"{settings.commission_reminder_hours} ч. с подачи. "
                    f"Реквизиты: {settings.payment_details or 'уточните у логиста'}",
                )
                _reminded.add(order.id)
            except RPCError as exc:
                log.warning("Не удалось напомнить водителю %s по заказу #%s: %s", driver.tg_user_id, order.id, exc)
