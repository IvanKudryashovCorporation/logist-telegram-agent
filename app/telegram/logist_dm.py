"""Переписка с логистом в его же «Избранном» (агент работает от его аккаунта)."""

import logging

from telethon import TelegramClient, events

from app.config import settings
from app.publishing.service import handle_confirmation_reply

log = logging.getLogger("agent.logist_dm")


def register_logist_dm_handlers(client: TelegramClient) -> None:
    if not settings.logist_user_id:
        return

    @client.on(events.NewMessage(chats=settings.logist_user_id))
    async def on_logist_message(event: events.NewMessage.Event) -> None:
        text = (event.raw_text or "").strip().lower()
        if not text:
            return
        handled = await handle_confirmation_reply(client, event.message.reply_to_msg_id, text)
        if not handled:
            log.debug("Сообщение логиста не распознано как ответ на подтверждение: %r", text)
