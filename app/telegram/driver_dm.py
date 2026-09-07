"""Личные сообщения от водителей — продолжение переговоров (вопрос 71)."""

import logging

from telethon import TelegramClient, events

from app.config import settings
from app.negotiation.service import handle_driver_dm

log = logging.getLogger("agent.driver_dm")


def register_driver_dm_handlers(client: TelegramClient) -> None:
    @client.on(events.NewMessage(incoming=True, func=lambda e: e.is_private))
    async def on_private_message(event: events.NewMessage.Event) -> None:
        if event.chat_id == settings.logist_user_id:
            return  # это переписка с логистом в «Избранном», её ведёт logist_dm
        try:
            await handle_driver_dm(client, event.sender_id, event.message)
        except Exception:
            log.exception("Ошибка обработки личного сообщения от %s", event.sender_id)
