"""Обработчики сообщений в рабочих группах диспетчеров: разбор заявок в БД.

Агрегатор для водителей — здесь только парсинг, никакой публикации или
переписки с кем-либо (вопросы 19-20, 103 из опроса, актуальны только
в части устройства парсинга).
"""

import logging
from datetime import timedelta

from telethon import TelegramClient, events

from app.city_aliases import expand_city_term
from app.db.base import SessionLocal
from app.models import ActionLog, ActorType, Order, OrderStatus
from app.parsing.llm_parser import parse_order_texts
from app.parsing.order_builder import apply_parsed_fields, combine_pickup_at
from app.parsing.schema import ParsedOrder
from app.telegram.dedup import mark_processed, unmark_processed
from sqlalchemy import select

log = logging.getLogger("agent.work_group")


# Диспетчеры иногда пишут дату подачи диапазоном ("29.09-30.09" — забрать
# в этот период) — LLM у разных сообщений/попыток может разобрать такой
# диапазон то как начало, то как конец, так что ТОЧНОЕ совпадение datetime
# пропускает реальные дубли. Сутки туда-сюда — тот же рейс.
_DUPLICATE_DATE_TOLERANCE = timedelta(days=1)


async def _find_duplicate(session, parsed: ParsedOrder) -> Order | None:
    """Тот же маршрут, подача в пределах суток И та же цена уже есть в
    активной ленте — разные диспетчеры иногда публикуют один и тот же рейс
    порознь, в разных группах, слегка другими словами (сокращённые города,
    диапазон дат вместо точной и т.п.), но с той же ценой. Цена —
    обязательное условие: одинаковый маршрут/время с РАЗНОЙ ценой — это два
    разных реальных предложения (разные диспетчеры, разные машины), не
    дубль, их нельзя схлопывать.

    Сравниваем города через тот же справочник сокращений, что и фильтр на
    сайте, а не как есть, — иначе "Симф" и "Симферополь" не совпадут.
    """
    pickup_at = combine_pickup_at(parsed.pickup_date, parsed.pickup_time)
    if not parsed.from_city or not parsed.to_city or pickup_at is None or parsed.client_price is None:
        return None

    from_canon = expand_city_term(parsed.from_city).lower()
    to_canon = expand_city_term(parsed.to_city).lower()

    candidates = (
        await session.execute(
            select(Order).where(
                Order.status != OrderStatus.CANCELLED,
                Order.pickup_at >= pickup_at - _DUPLICATE_DATE_TOLERANCE,
                Order.pickup_at <= pickup_at + _DUPLICATE_DATE_TOLERANCE,
                Order.client_price == parsed.client_price,
            )
        )
    ).scalars().all()

    for candidate in candidates:
        if not candidate.from_city or not candidate.to_city:
            continue
        if (
            expand_city_term(candidate.from_city).lower() == from_canon
            and expand_city_term(candidate.to_city).lower() == to_canon
        ):
            return candidate
    return None


def register_work_group_handlers(client: TelegramClient, chat_ids: list[int]) -> None:
    if not chat_ids:
        return

    @client.on(events.NewMessage(chats=chat_ids))
    async def on_new_order_message(event: events.NewMessage.Event) -> None:
        await _handle_message(event.message, is_edit=False)

    @client.on(events.MessageEdited(chats=chat_ids))
    async def on_edited_order_message(event: events.MessageEdited.Event) -> None:
        await _handle_message(event.message, is_edit=True)

    @client.on(events.MessageDeleted(chats=chat_ids))
    async def on_deleted_order_message(event: events.MessageDeleted.Event) -> None:
        await _handle_deleted(event.chat_id, event.deleted_ids)


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


async def _handle_deleted(chat_id: int | None, message_ids: list[int]) -> None:
    """Диспетчер удалил сообщение в Telegram — заявки из него больше не
    актуальны, скрываем со дна ленты (как и любую вручную отменённую
    заявку). У водителей, которые уже взяли заказ себе, он остаётся виден
    в "Моих заказах" — они уже договорились, пропадает только из общей ленты.
    """
    if chat_id is None or not message_ids:
        return

    async with SessionLocal() as session:
        orders = (
            await session.execute(
                select(Order).where(
                    Order.source_chat_id == chat_id,
                    Order.source_message_id.in_(message_ids),
                    Order.status != OrderStatus.CANCELLED,
                )
            )
        ).scalars().all()
        if not orders:
            return

        for order in orders:
            order.status = OrderStatus.CANCELLED
            session.add(
                ActionLog(
                    order_id=order.id,
                    actor=ActorType.DISPATCHER,
                    action="cancelled_message_deleted",
                )
            )
        await session.commit()

        log.info(
            "Сообщение удалено в Telegram (chat=%s) — скрыто заказов: %s",
            chat_id,
            [o.id for o in orders],
        )


def _order_raw_text(parsed: ParsedOrder, fallback_text: str) -> str:
    """Кусок исходного текста, относящийся именно к этой заявке (для
    "Исходный текст заявки" на сайте) — если LLM не вернула фрагмент
    (сообщение с одной заявкой), берём весь текст сообщения."""
    snippet = (parsed.raw_snippet or "").strip()
    return snippet if snippet else fallback_text


async def _upsert_order(message, is_edit: bool) -> list[int]:
    """Возвращает id всех созданных/обновлённых заказов из этого сообщения
    (может быть несколько, если в сообщении несколько заявок; пустой список,
    если сообщение не было заявкой вовсе)."""
    text = message.raw_text.strip()
    sender = await message.get_sender()
    dispatcher_tg_id = getattr(sender, "id", None)
    dispatcher_username = getattr(sender, "username", None)

    async with SessionLocal() as session:
        existing_by_index = {
            o.source_sub_index: o
            for o in (
                await session.execute(
                    select(Order).where(
                        Order.source_chat_id == message.chat_id,
                        Order.source_message_id == message.id,
                    )
                )
            ).scalars().all()
        }
        if is_edit and not existing_by_index:
            return []  # правка сообщения, которое мы и раньше не считали заявкой

        parsed_list = await parse_order_texts(text)

        if not parsed_list:
            if is_edit and existing_by_index:
                # Правка убрала из сообщения все заявки (переписал текст на
                # обычное сообщение) — прежде отслеживаемые больше не актуальны.
                for order in existing_by_index.values():
                    if order.status != OrderStatus.CANCELLED:
                        order.status = OrderStatus.CANCELLED
                        session.add(
                            ActionLog(
                                order_id=order.id,
                                actor=ActorType.DISPATCHER,
                                actor_tg_id=dispatcher_tg_id,
                                action="cancelled_edited_out",
                            )
                        )
                await session.commit()
            return []  # обычное сообщение в чате, не заявка

        result_ids: list[int] = []

        for index, parsed in enumerate(parsed_list):
            existing = existing_by_index.get(index)
            is_new_order = existing is None

            if is_new_order:
                duplicate = await _find_duplicate(session, parsed)
                if duplicate is not None:
                    session.add(
                        ActionLog(
                            order_id=duplicate.id,
                            actor=ActorType.DISPATCHER,
                            actor_tg_id=dispatcher_tg_id,
                            action="duplicate_skipped",
                            details=f"chat={message.chat_id} msg={message.id} sub={index}",
                        )
                    )
                    await session.commit()
                    log.info(
                        "Дубль заказа #%s пропущен: %s -> %s (chat=%s msg=%s sub=%s)",
                        duplicate.id,
                        duplicate.from_city,
                        duplicate.to_city,
                        message.chat_id,
                        message.id,
                        index,
                    )
                    result_ids.append(duplicate.id)
                    continue

                order = Order(
                    source_chat_id=message.chat_id,
                    source_message_id=message.id,
                    source_sub_index=index,
                    dispatcher_tg_id=dispatcher_tg_id,
                    dispatcher_username=dispatcher_username,
                    raw_text=_order_raw_text(parsed, text),
                )
                session.add(order)
            else:
                order = existing
                order.raw_text = _order_raw_text(parsed, text)

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
            result_ids.append(order.id)

            log.info(
                "%s заказ #%s: %s -> %s, статус=%s",
                "Создан" if is_new_order else "Обновлён",
                order.id,
                order.from_city,
                order.to_city,
                order.status.value,
            )

        # Правка сократила число заявок в сообщении — лишние из прошлой
        # версии больше не актуальны.
        for leftover_index, leftover_order in existing_by_index.items():
            if leftover_index >= len(parsed_list) and leftover_order.status != OrderStatus.CANCELLED:
                leftover_order.status = OrderStatus.CANCELLED
                session.add(
                    ActionLog(
                        order_id=leftover_order.id,
                        actor=ActorType.DISPATCHER,
                        actor_tg_id=dispatcher_tg_id,
                        action="cancelled_edited_out",
                    )
                )

        await session.commit()
        return result_ids
