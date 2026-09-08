"""Исполнитель задач из очереди веб-панели (см. app/models/pending_action.py).

Работает внутри процесса агента, у которого есть живой Telethon-клиент —
веб-панель сама Telegram не трогает.
"""

import logging
from datetime import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select
from telethon import TelegramClient

from app.db.base import SessionLocal
from app.models import PendingAction, PendingActionStatus, PendingActionType
from app.publishing.service import publish_order
from app.workflow.service import assign_driver, cancel_order, complete_order, confirm_payment

log = logging.getLogger("agent.workflow.queue")

_POLL_INTERVAL_SECONDS = 8


def start_pending_actions_poller(client: TelegramClient) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler()
    scheduler.add_job(_process_pending_actions, "interval", seconds=_POLL_INTERVAL_SECONDS, args=[client])
    scheduler.start()
    log.info("Очередь действий из веб-панели: проверка каждые %s сек.", _POLL_INTERVAL_SECONDS)
    return scheduler


async def _process_pending_actions(client: TelegramClient) -> None:
    async with SessionLocal() as session:
        pending_ids = (
            await session.execute(
                select(PendingAction.id)
                .where(PendingAction.status == PendingActionStatus.PENDING)
                .order_by(PendingAction.created_at)
            )
        ).scalars().all()

    for action_id in pending_ids:
        await _process_one(client, action_id)


async def _process_one(client: TelegramClient, action_id: int) -> None:
    async with SessionLocal() as session:
        action = await session.get(PendingAction, action_id)
        if action is None or action.status != PendingActionStatus.PENDING:
            return
        order_id, response_id, action_type = action.order_id, action.response_id, action.action

    try:
        if action_type == PendingActionType.PUBLISH:
            await publish_order(order_id, client)
            result = "Опубликовано (подробности — в сообщении логисту в Telegram)."
        elif action_type == PendingActionType.ASSIGN:
            result = await assign_driver(client, order_id, response_id)
        elif action_type == PendingActionType.COMPLETE:
            result = await complete_order(client, order_id)
        elif action_type == PendingActionType.CANCEL:
            result = await cancel_order(client, order_id)
        elif action_type == PendingActionType.CONFIRM_PAYMENT:
            result = await confirm_payment(client, order_id)
        else:
            result = f"Неизвестное действие: {action_type}"
        status = PendingActionStatus.DONE
    except Exception as exc:
        log.exception("Ошибка обработки задачи #%s (%s) для заказа #%s", action_id, action_type, order_id)
        result = f"Ошибка: {exc}"
        status = PendingActionStatus.FAILED

    async with SessionLocal() as session:
        action = await session.get(PendingAction, action_id)
        if action is None:
            return
        action.status = status
        action.result_message = result
        action.processed_at = datetime.utcnow()
        await session.commit()
