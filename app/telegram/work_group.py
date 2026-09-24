"""Обработчики сообщений в рабочих группах диспетчеров: разбор заявок в БД.

Агрегатор для водителей — здесь только парсинг, никакой публикации или
переписки с кем-либо (вопросы 19-20, 103 из опроса, актуальны только
в части устройства парсинга).
"""

import logging

from telethon import TelegramClient, events

from app.db.base import SessionLocal
from app.models import ActionLog, ActorType, Order
from app.parsing.llm_parser import parse_order_text
from app.parsing.order_builder import apply_parsed_fields
from app.telegram.dedup import mark_processed, unmark_processed
from sqlalchemy import select

log = logging.getLogger("agent.work_group")


def register_work_group_handlers(client: TelegramClient, chat_ids: list[int]) -> None:
    if not chat_ids:
        return

    @client.on(events.NewMessage(chats=chat_ids))
    async def on_new_order_message(event: events.NewMessage.Event) -> None:
        await _handle_message(event.message, is_edit=False)

    @client.on(events.MessageEdited(chats=chat_ids))
    async def on_edited_order_message(event: events.MessageEdited.Event) -> None:
        await _handle_message(event.message, is_edit=True)


async def _handle_message(message, is_edit: bool) -> None:
    if message.out:
        return  # собственные сообщения агента в эту группу — не заявки

    text = (message.raw_text or "").strip()
    if not text:
        return

    if not is_edit and message.reply_to_msg_id is not None:
        # Реплай — это переписка в чате, а не новая заявка.
        return

    # Защита от повторной доставки одного и того же события Telethon (реконнект,
    # get_difference) — иначе получаем дубликат заказа.
    if not mark_processed(message.chat_id, message.id):
        return

    try:
        await _upsert_order(message, is_edit)
    except Exception:
        unmark_processed(message.chat_id, message.id)
        raise


async def _upsert_order(message, is_edit: bool) -> int | None:
    """Возвращает id созданного/обновлённого заказа, или None, если сообщение
    не было заявкой (обычная переписка) либо это правка того, что и раньше не было заявкой."""
    text = message.raw_text.strip()
    sender = await message.get_sender()
    dispatcher_tg_id = getattr(sender, "id", None)
    dispatcher_username = getattr(sender, "username", None)

    async with SessionLocal() as session:
        order = (
            await session.execute(
                select(Order).where(
                    Order.source_chat_id == message.chat_id,
                    Order.source_message_id == message.id,
                )
            )
        ).scalar_one_or_none()
        if is_edit and order is None:
            return  # правка сообщения, которое мы и раньше не считали заявкой

        parsed = await parse_order_text(text)

        is_new_order = order is None
        if order is None:
            if not parsed.is_order:
                return  # обычное сообщение в чате, не заявка
            order = Order(
                source_chat_id=message.chat_id,
                source_message_id=message.id,
                dispatcher_tg_id=dispatcher_tg_id,
                dispatcher_username=dispatcher_username,
                raw_text=text,
            )
            session.add(order)
        else:
            order.raw_text = text

        apply_parsed_fields(order, parsed)
        await session.flush()

        session.add(
            ActionLog(
                order_id=order.id,
                actor=ActorType.DISPATCHER,
                actor_tg_id=dispatcher_tg_id,
                action="order_created" if is_new_order else "order_edited",
                details=f"missing_fields={parsed.missing_fields}" if parsed.missing_fields else None,
            )
        )
        await session.commit()

        log.info(
            "%s заказ #%s: %s -> %s, статус=%s",
            "Создан" if is_new_order else "Обновлён",
            order.id,
            order.from_city,
            order.to_city,
            order.status.value,
        )
        return order.id
