"""Обработчики сообщений в рабочей группе диспетчеров (Этап 1, вопросы 19-20, 103)."""

import logging

from telethon import TelegramClient, events

from app.config import settings
from app.db.base import SessionLocal
from app.models import ActionLog, ActorType, Order, OrderStatus
from app.parsing.llm_parser import parse_order_text
from app.parsing.order_builder import apply_parsed_fields
from app.publishing.service import propose_publication
from sqlalchemy import select

log = logging.getLogger("agent.work_group")


def register_work_group_handlers(client: TelegramClient) -> None:
    if not settings.work_group_chat_id:
        return

    @client.on(events.NewMessage(chats=settings.work_group_chat_id))
    async def on_new_order_message(event: events.NewMessage.Event) -> None:
        await _handle_message(client, event.message, is_edit=False)

    @client.on(events.MessageEdited(chats=settings.work_group_chat_id))
    async def on_edited_order_message(event: events.MessageEdited.Event) -> None:
        await _handle_message(client, event.message, is_edit=True)


async def _handle_message(client: TelegramClient, message, is_edit: bool) -> None:
    text = (message.raw_text or "").strip()
    if not text:
        return

    if not is_edit and message.reply_to_msg_id is not None:
        # Реплай — это переписка (например отклик водителя в общем чате), а не новая заявка.
        return

    sender = await message.get_sender()
    dispatcher_tg_id = getattr(sender, "id", None)
    dispatcher_username = getattr(sender, "username", None)

    async with SessionLocal() as session:
        order = None
        if is_edit:
            order = (
                await session.execute(
                    select(Order).where(
                        Order.source_chat_id == message.chat_id,
                        Order.source_message_id == message.id,
                    )
                )
            ).scalar_one_or_none()
            if order is None:
                return  # правка сообщения, которое мы и раньше не считали заявкой

        parsed = await parse_order_text(text)

        is_new_order = order is None
        if order is None:
            if not parsed.is_order:
                return  # обычное сообщение в чате, не заявка — не заводим запись
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

        status_before = order.status
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
        order_id, order_status = order.id, order.status

        log.info(
            "%s заказ #%s: %s -> %s, статус=%s",
            "Создан" if is_new_order else "Обновлён",
            order_id,
            order.from_city,
            order.to_city,
            order_status.value,
        )

    # Предлагаем публикацию, когда заказ впервые становится полностью разобранным.
    if order_status == OrderStatus.NEW and (is_new_order or status_before == OrderStatus.NEEDS_CLARIFICATION):
        await propose_publication(order_id, client)
