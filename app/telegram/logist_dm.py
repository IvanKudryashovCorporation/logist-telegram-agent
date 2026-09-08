"""Переписка с логистом в его же «Избранном» (агент работает от его аккаунта)."""

import logging

from telethon import TelegramClient, events

from app.config import settings
from app.publishing.service import handle_confirmation_reply
from app.workflow.service import assign_driver, cancel_order, complete_order, confirm_payment

log = logging.getLogger("agent.logist_dm")

_HELP = (
    "Команды:\n"
    "/assign <order_id> [response_id] — назначить водителя\n"
    "/complete <order_id> — завершить заказ\n"
    "/cancel <order_id> — отменить заказ\n"
    "/paid <order_id> — подтвердить получение комиссии"
)


def register_logist_dm_handlers(client: TelegramClient) -> None:
    if not settings.logist_user_id:
        return

    @client.on(events.NewMessage(chats=settings.logist_user_id))
    async def on_logist_message(event: events.NewMessage.Event) -> None:
        raw = (event.raw_text or "").strip()
        if not raw:
            return

        if raw.startswith("/"):
            await _handle_command(client, raw)
            return

        text = raw.lower()
        handled = await handle_confirmation_reply(client, event.message.reply_to_msg_id, text)
        if not handled:
            log.debug("Сообщение логиста не распознано как ответ на подтверждение: %r", text)


async def _handle_command(client: TelegramClient, raw: str) -> None:
    parts = raw.split()
    command = parts[0].lower()

    try:
        if command == "/assign":
            order_id = int(parts[1])
            response_id = int(parts[2]) if len(parts) > 2 else None
            reply = await assign_driver(client, order_id, response_id)
        elif command == "/complete":
            reply = await complete_order(client, int(parts[1]))
        elif command == "/cancel":
            reply = await cancel_order(client, int(parts[1]))
        elif command == "/paid":
            reply = await confirm_payment(client, int(parts[1]))
        elif command in ("/help", "/start"):
            reply = _HELP
        else:
            reply = f"Неизвестная команда: {command}\n\n{_HELP}"
    except (IndexError, ValueError):
        reply = f"Не разобрал команду.\n\n{_HELP}"

    await client.send_message(settings.logist_user_id, reply)
