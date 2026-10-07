"""Кнопка «Взять заказ» под уведомлением бота.

Нажатие приходит боту как ``callback_query`` с данными ``take:<id заказа>``. Кто нажал, сообщает
сам Telegram (подделать нельзя), поэтому тот же атомарный :func:`app.web.queries.take_order`,
что и на сайте, закрепляет заказ за ``tg:<id>``: он пропадает из общей ленты и появляется в
«Мои заказы». Контакты клиента, как и на сайте, открываются только после этого.

После нажатия сообщение превращается в подтверждение с кнопками «Написать диспетчеру» и
«Открыть заказ». Если в сообщении был список из нескольких заказов, то в нём убирается только
строка с этим заказом, а подтверждение приходит отдельным сообщением.
"""

import logging
from typing import Awaitable, Callable, Optional

from sqlalchemy import select

from app.config import settings
from app.db.base import SessionLocal
from app.models import Driver, Order
from app.services.subscriptions import chat_url, order_line, order_url
from app.web import queries

log = logging.getLogger("web.bot_orders")

#: ``api(method, payload) -> result`` — вызов Bot API (в тестах подменяется).
BotApi = Callable[[str, dict], Awaitable[Optional[dict]]]

TAKE_PREFIX = "take:"


def _take_buttons(markup: Optional[dict]) -> list[str]:
    return [
        button["callback_data"]
        for row in (markup or {}).get("inline_keyboard", [])
        for button in row
        if str(button.get("callback_data", "")).startswith(TAKE_PREFIX)
    ]


def _without_order(markup: dict, callback_data: str) -> dict:
    """Клавиатура без строки с этим заказом."""
    rows = [
        row for row in markup.get("inline_keyboard", [])
        if not any(button.get("callback_data") == callback_data for button in row)
    ]
    return {"inline_keyboard": rows}


def confirmation(order: Order, group_username: Optional[str]) -> tuple[str, dict]:
    """Текст и кнопки подтверждения «Заказ ваш»."""
    text = (
        "✅ Заказ ваш\n\n"
        f"{order_line(order)}\n\n"
        "Имя и телефон клиента — на сайте, в разделе «Мои заказы». Свяжитесь с диспетчером, а когда "
        "договоритесь, отметьте это на сайте."
    )
    row: list[dict] = []
    url = chat_url(order, group_username)
    if url:
        row.append({"text": "✉️ Написать диспетчеру", "url": url})
    row.append({"text": "Открыть заказ", "url": order_url(order)})
    return text, {"inline_keyboard": [row]}


async def handle_take(callback: dict, api: BotApi) -> None:
    """Обрабатывает нажатие «Взять заказ»."""

    async def answer(text: str, *, alert: bool = False) -> None:
        await api(
            "answerCallbackQuery",
            {"callback_query_id": callback["id"], "text": text[:190], "show_alert": alert},
        )

    try:
        order_id = int(str(callback.get("data", "")).removeprefix(TAKE_PREFIX))
    except ValueError:
        await answer("Не удалось определить заказ.", alert=True)
        return

    user_id = callback["from"]["id"]
    async with SessionLocal() as session:
        known = (
            await session.execute(select(Driver.id).where(Driver.telegram_id == user_id))
        ).scalar_one_or_none()
        if known is None:
            await answer(f"Сначала войдите на сайт через Telegram: {settings.public_base_url}", alert=True)
            return
        result = await queries.take_order(session, order_id, f"tg:{user_id}")
        order = await session.get(Order, order_id)
        group = await queries.group_username(session, order.source_chat_id) if order else None

    if not (result.ok or result.reason == "already_mine"):
        await answer(result.message or "Не удалось взять заказ.", alert=True)
        return

    log.info("Заказ #%s взят из бота: tg:%s", order_id, user_id)
    await answer("Заказ ваш ✅")

    message = callback.get("message") or {}
    chat_id, message_id = message.get("chat", {}).get("id"), message.get("message_id")
    if chat_id is None or message_id is None or order is None:
        return
    text, markup = confirmation(order, group)
    markup_in_message = message.get("reply_markup")
    if len(_take_buttons(markup_in_message)) <= 1:
        # Уведомление об одном заказе — само становится подтверждением.
        await api(
            "editMessageText",
            {"chat_id": chat_id, "message_id": message_id, "text": text, "reply_markup": markup,
             "disable_web_page_preview": True},
        )
        return
    # Список заказов: у остальных кнопки остаются, подтверждение приходит отдельным сообщением.
    await api(
        "editMessageReplyMarkup",
        {"chat_id": chat_id, "message_id": message_id,
         "reply_markup": _without_order(markup_in_message, f"{TAKE_PREFIX}{order_id}")},
    )
    await api(
        "sendMessage",
        {"chat_id": chat_id, "text": text, "reply_markup": markup, "disable_web_page_preview": True},
    )
