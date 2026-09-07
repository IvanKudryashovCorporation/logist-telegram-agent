"""Реплаи водителей на объявления в их группах (вопрос 68)."""

import logging

from telethon import TelegramClient, events

from app.negotiation.service import handle_group_reply

log = logging.getLogger("agent.driver_groups")


def register_driver_group_handlers(client: TelegramClient, chat_ids: list[int]) -> None:
    if not chat_ids:
        return

    @client.on(events.NewMessage(chats=chat_ids))
    async def on_group_message(event: events.NewMessage.Event) -> None:
        if event.message.reply_to_msg_id is None:
            return
        try:
            await handle_group_reply(client, event.chat_id, event.message)
        except Exception:
            log.exception("Ошибка обработки отклика в группе %s", event.chat_id)
