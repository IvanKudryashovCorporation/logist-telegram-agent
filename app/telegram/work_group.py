"""Обработчики сообщений в рабочих группах диспетчеров: разбор заявок в БД.

Агрегатор для водителей — здесь только парсинг, никакой публикации или
переписки с кем-либо.

Устройство после ревизии надёжности:

* :func:`upsert_order_text` — ядро разбора. Оно НЕ знает про Telethon и
  принимает обычные значения (chat_id, message_id, текст, отправитель), поэтому
  его может вызвать и обработчик события, и воркер очереди повторного разбора.
* Перед обращением к LLM текст проходит :func:`app.parsing.prefilter.prefilter`
  — переписка в чате не стоит денег и не создаёт риск 429.
* Если LLM всё же отказала (:class:`ParseUnavailable`), сообщение уходит в
  очередь ``pending_messages``, а не теряется.
* Каждая обработка пишет строку в ``parse_stats`` — без этого невозможно
  понять, не «врёт» ли модель и сколько тратим токенов.
"""

import asyncio
import logging
from datetime import timedelta
from typing import Optional

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from telethon import TelegramClient, events

from app.city_aliases import canonical_city_variants, city_key, expand_city_term
from app.config import settings
from app.db.base import SessionLocal
from app.models import (
    HIDDEN_STATUSES,
    ActionLog,
    ActorType,
    Order,
    OrderStatus,
    ParseOutcome,
)
from app.parsing import stats as parse_stats
from app.parsing.llm_parser import parse_orders, text_hash
from app.parsing.order_builder import apply_parsed_fields, combine_pickup_at
from app.parsing.prefilter import PrefilterDecision, prefilter
from app.parsing.schema import ParsedOrder
from app.telegram import pending as pending_queue
from app.telegram.dedup import mark_processed, unmark_processed

log = logging.getLogger("agent.work_group")


# Диспетчеры иногда пишут дату подачи диапазоном ("29.09-30.09" — забрать
# в этот период) — LLM у разных сообщений/попыток может разобрать такой
# диапазон то как начало, то как конец, так что ТОЧНОЕ совпадение datetime
# пропускает реальные дубли. Сутки туда-сюда — тот же рейс.
_DUPLICATE_DATE_TOLERANCE = timedelta(days=1)

# Telethon раздаёт события НЕЗАВИСИМЫМИ задачами — сообщения из разных групп,
# пришедшие почти одновременно, разбираются конкурентно, каждое в своей сессии
# БД. _find_duplicate — это SELECT, а не атомарный UPDATE (нельзя вставить
# заказ "только если такого ещё нет" одним запросом), поэтому без блокировки
# два конкурентных вызова читают "дубликатов нет" ДО того, как первый из них
# закоммитит INSERT — оба создают заказ, дубль всё равно проскакивает.
# Сериализуем именно проверку+запись (не разбор LLM — он медленный, но гонки
# не создаёт), чтобы commit одного вызова был виден SELECT'у следующего.
_dedup_lock = asyncio.Lock()


def _order_raw_text(parsed: ParsedOrder, fallback_text: str) -> str:
    """Кусок исходного текста, относящийся именно к этой заявке (для
    "Исходный текст заявки" на сайте) — если LLM не вернула фрагмент
    (сообщение с одной заявкой), берём весь текст сообщения."""
    snippet = (parsed.raw_snippet or "").strip()
    return snippet if snippet else fallback_text


def _city_match_clauses(column, raw_city: str):
    """SQL-условие «город заказа совпадает с указанным» — с учётом сокращений.

    Работает по подготовленной колонке ``*_city_key`` (нижний регистр, без
    «г.»): точное вхождение любого известного написания ИЛИ подстрока от
    достаточно длинных вариантов. Подстрока нужна, потому что LLM пишет и
    «Симферополь, аэропорт» — точное равенство такой город не поймало бы.
    """
    variants = canonical_city_variants(raw_city)
    if not variants:
        return None
    clauses = [column.in_(variants)]
    clauses += [column.contains(v) for v in variants if len(v) >= 4]
    return or_(*clauses)


async def _find_duplicate(session, parsed: ParsedOrder) -> Optional[Order]:
    """Тот же маршрут, подача в пределах суток И та же цена уже есть в
    активной ленте — разные диспетчеры иногда публикуют один и тот же рейс
    порознь, в разных группах, слегка другими словами (сокращённые города,
    диапазон дат вместо точной и т.п.), но с той же ценой. Цена —
    обязательное условие: одинаковый маршрут/время с РАЗНОЙ ценой — это два
    разных реальных предложения (разные диспетчеры, разные машины), не
    дубль, их нельзя схлопывать.

    Города сравниваются прямо в SQL по подготовленным ``*_city_key`` — раньше
    подтягивались ВСЕ заказы из окна дат и перебирались в Python.
    Закрытые заказы (CANCELLED/AGREED/EXPIRED) дублями не считаются: заявка
    уже отработана, свежая публикация того же рейса — новая возможность.
    """
    pickup_at = combine_pickup_at(parsed.pickup_date, parsed.pickup_time)
    if not parsed.from_city or not parsed.to_city or pickup_at is None or parsed.client_price is None:
        return None

    from_clause = _city_match_clauses(Order.from_city_key, parsed.from_city)
    to_clause = _city_match_clauses(Order.to_city_key, parsed.to_city)
    if from_clause is None or to_clause is None:
        return None

    candidates = (
        await session.execute(
            select(Order)
            .where(
                Order.status.notin_(HIDDEN_STATUSES),
                Order.pickup_at >= pickup_at - _DUPLICATE_DATE_TOLERANCE,
                Order.pickup_at <= pickup_at + _DUPLICATE_DATE_TOLERANCE,
                Order.client_price == parsed.client_price,
                from_clause,
                to_clause,
            )
            .order_by(Order.id.asc())
            .limit(5)
        )
    ).scalars().all()

    # Страховка от слишком широкого LIKE: финальная сверка канонических городов.
    from_canon = city_key(expand_city_term(parsed.from_city))
    to_canon = city_key(expand_city_term(parsed.to_city))
    for candidate in candidates:
        if city_key(candidate.from_city) == from_canon and city_key(candidate.to_city) == to_canon:
            return candidate
    return candidates[0] if candidates else None


async def upsert_order_text(
    *,
    chat_id: int,
    message_id: int,
    text: str,
    dispatcher_tg_id: Optional[int] = None,
    dispatcher_username: Optional[str] = None,
    is_edit: bool = False,
) -> list[int]:
    """Разбирает текст сообщения и создаёт/обновляет заказы.

    Возвращает id всех созданных или обновлённых заказов (пустой список —
    сообщение не заявка). Не зависит от Telethon: воркер очереди вызывает
    эту же функцию для отложенных сообщений.

    Сессия всегда своя и всегда коммитится здесь. Это осознанно: разбор
    обязан быть идемпотентным (unique на источник защищает от дублей), а
    общая транзакция с вызывающим кодом привела бы к тому, что rollback при
    IntegrityError откатывал бы и посторонние изменения.

    Бросает :class:`ParseUnavailable`, если LLM недоступна, — вызывающий код
    обязан поставить сообщение в очередь.
    """
    text = (text or "").strip()
    if not text:
        return []

    async with SessionLocal() as db:
        existing_by_index = {
            order.source_sub_index: order
            for order in (
                await db.execute(
                    select(Order).where(
                        Order.source_chat_id == chat_id,
                        Order.source_message_id == message_id,
                    )
                )
            ).scalars().all()
        }

        decision: PrefilterDecision = (
            prefilter(text)
            if settings.prefilter_enabled
            else PrefilterDecision(True, "prefilter_disabled")
        )

        if not decision.send_to_llm:
            if is_edit and existing_by_index:
                # Правка превратила заявку в обычную переписку — снимаем заказы,
                # иначе в ленте останется то, чего диспетчер уже не предлагает.
                await _cancel_orders(db, existing_by_index.values(), dispatcher_tg_id,
                                     action="cancelled_edited_out")
            await parse_stats.record(
                db,
                ParseOutcome.PREFILTERED,
                chat_id=chat_id,
                message_id=message_id,
                text_hash=text_hash(text),
                error=decision.reason,
            )
            await db.commit()
            log.debug(
                "Предфильтр отсеял сообщение chat=%s msg=%s (%s)", chat_id, message_id, decision.reason
            )
            return []

        parse_result = await parse_orders(text)
        parsed_list = parse_result.orders

        if not parsed_list:
            if is_edit and existing_by_index:
                # Правка убрала из сообщения все заявки — отслеживаемые больше не актуальны.
                await _cancel_orders(db, existing_by_index.values(), dispatcher_tg_id,
                                     action="cancelled_edited_out")
            await parse_stats.record(
                db,
                ParseOutcome.NOT_ORDER,
                chat_id=chat_id,
                message_id=message_id,
                result=parse_result,
            )
            await db.commit()
            return []

        missing_total = sum(len(p.missing_fields) for p in parsed_list)
        # Держим лок до commit включительно — иначе SELECT дубля в соседней
        # задаче видит "чисто" ещё не закоммиченную вставку этой (см. комментарий
        # у _dedup_lock).
        async with _dedup_lock:
            result_ids = await _save_orders(
                db,
                chat_id=chat_id,
                message_id=message_id,
                text=text,
                parsed_list=parsed_list,
                existing_by_index=existing_by_index,
                dispatcher_tg_id=dispatcher_tg_id,
                dispatcher_username=dispatcher_username,
            )

            await parse_stats.record(
                db,
                ParseOutcome.ORDER,
                chat_id=chat_id,
                message_id=message_id,
                result=parse_result,
                orders_found=len(parsed_list),
                missing_fields=missing_total,
            )
            await db.commit()
        return result_ids


async def _cancel_orders(db, orders, dispatcher_tg_id: Optional[int], *, action: str) -> None:
    """Снимает заказы с ленты (правка/удаление сообщения в Telegram)."""
    for order in orders:
        if order.status == OrderStatus.CANCELLED:
            continue
        order.status = OrderStatus.CANCELLED
        db.add(
            ActionLog(
                order_id=order.id,
                actor=ActorType.DISPATCHER,
                actor_tg_id=dispatcher_tg_id,
                action=action,
            )
        )


async def _save_orders(
    db,
    *,
    chat_id: int,
    message_id: int,
    text: str,
    parsed_list: list[ParsedOrder],
    existing_by_index: dict[int, Order],
    dispatcher_tg_id: Optional[int],
    dispatcher_username: Optional[str],
) -> list[int]:
    """Создаёт/обновляет заказы по разобранному списку заявок. Коммитит вызывающий код."""
    result_ids: list[int] = []

    for index, parsed in enumerate(parsed_list):
        existing = existing_by_index.get(index)
        is_new_order = existing is None

        if is_new_order:
            duplicate = await _find_duplicate(db, parsed)
            if duplicate is not None:
                db.add(
                    ActionLog(
                        order_id=duplicate.id,
                        actor=ActorType.DISPATCHER,
                        actor_tg_id=dispatcher_tg_id,
                        action="duplicate_skipped",
                        details=f"chat={chat_id} msg={message_id} sub={index}",
                    )
                )
                await db.flush()
                await parse_stats.record(
                    db,
                    ParseOutcome.DUPLICATE,
                    chat_id=chat_id,
                    message_id=message_id,
                    orders_found=1,
                    error=f"duplicate_of={duplicate.id}",
                )
                log.info(
                    "Дубль заказа #%s пропущен: %s -> %s (chat=%s msg=%s sub=%s)",
                    duplicate.id, duplicate.from_city, duplicate.to_city,
                    chat_id, message_id, index,
                )
                result_ids.append(duplicate.id)
                continue

            order = Order(
                source_chat_id=chat_id,
                source_message_id=message_id,
                source_sub_index=index,
                dispatcher_tg_id=dispatcher_tg_id,
                dispatcher_username=dispatcher_username,
                raw_text=_order_raw_text(parsed, text),
            )
            db.add(order)
        else:
            order = existing
            order.raw_text = _order_raw_text(parsed, text)

        apply_parsed_fields(order, parsed)
        try:
            await db.flush()
        except IntegrityError:
            # Событие Telethon переиграли после перезапуска агента: заявка из
            # этого (chat, message, sub_index) уже в БД. Unique-constraint
            # отработал как надо — просто не создаём дубль.
            await db.rollback()
            log.warning(
                "Заявка chat=%s msg=%s sub=%s уже есть в БД — повторная запись пропущена",
                chat_id, message_id, index,
            )
            continue

        db.add(
            ActionLog(
                order_id=order.id,
                actor=ActorType.DISPATCHER,
                actor_tg_id=dispatcher_tg_id,
                action="order_created" if is_new_order else "order_edited",
                details=(
                    f"missing_fields={parsed.missing_fields}" if parsed.missing_fields else None
                ),
            )
        )
        result_ids.append(order.id)

        log.info(
            "%s заказ #%s: %s -> %s, статус=%s",
            "Создан" if is_new_order else "Обновлён",
            order.id, order.from_city, order.to_city, order.status.value,
        )

    # Правка сократила число заявок в сообщении — лишние из прошлой
    # версии больше не актуальны.
    leftovers = [
        order for idx, order in existing_by_index.items() if idx >= len(parsed_list)
    ]
    await _cancel_orders(db, leftovers, dispatcher_tg_id, action="cancelled_edited_out")

    return result_ids


# ---------------------------------------------------------------------------
# Обработчики событий Telethon
# ---------------------------------------------------------------------------


def _dedup_variant(message, is_edit: bool) -> str:
    """Ключ варианта события: новое сообщение или конкретная его правка.

    ``edit_date`` уникален для каждой правки, поэтому повторная доставка того
    же события по-прежнему отсекается, а НОВАЯ правка обрабатывается. Без
    этого диспетчер мог добавить в сообщение цену или исправить город — а в
    ленте оставалась старая версия.
    """
    if not is_edit:
        return "new"
    edit_date = getattr(message, "edit_date", None)
    stamp = int(edit_date.timestamp()) if edit_date is not None else 0
    return f"edit:{stamp}"


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
    """Точка входа для события Telethon.

    Главное отличие от прежней версии: исключение здесь НЕ означает потерю
    сообщения. Оно ставится в очередь ``pending_messages``, и воркер повторит
    разбор позже. Исключение наружу не пробрасываем — иначе Telethon зальёт
    лог одним и тем же стектрейсом на каждое событие, а заявка всё равно
    никуда не денется.
    """
    if message.out:
        return  # собственные сообщения агента в эту группу — не заявки

    text = (message.raw_text or "").strip()
    if not text:
        return

    if not is_edit and message.reply_to_msg_id is not None:
        # Реплай — это переписка в чате, а не новая заявка.
        return

    variant = _dedup_variant(message, is_edit)
    if not mark_processed(message.chat_id, message.id, variant):
        return

    try:
        sender = await message.get_sender()
        await upsert_order_text(
            chat_id=message.chat_id,
            message_id=message.id,
            text=text,
            dispatcher_tg_id=getattr(sender, "id", None),
            dispatcher_username=getattr(sender, "username", None),
            is_edit=is_edit,
        )
    except Exception as exc:
        unmark_processed(message.chat_id, message.id, variant)
        detail = f"{type(exc).__name__}: {exc}"
        log.exception(
            "Не удалось разобрать сообщение chat=%s msg=%s — ставим в очередь",
            message.chat_id, message.id,
        )
        await pending_queue.enqueue(
            chat_id=message.chat_id,
            message_id=message.id,
            text=text,
            is_edit=is_edit,
            error=detail,
        )
        await parse_stats.record(
            None,
            ParseOutcome.QUEUED,
            chat_id=message.chat_id,
            message_id=message.id,
            text_hash=text_hash(text),
            error=detail,
        )


async def _handle_deleted(chat_id: Optional[int], message_ids: list[int]) -> None:
    """Диспетчер удалил сообщение в Telegram — заявки из него больше не
    актуальны, скрываем со дна ленты (как и любую вручную отменённую
    заявку). У водителей, которые уже взяли заказ себе, он остаётся виден
    в "Моих заказах" — они уже договорились, пропадает только из общей ленты.

    Заказы со статусом AGREED не трогаем: сделка уже закрыта, и подмена
    статуса на CANCELLED стерла бы этот факт из истории.
    """
    if chat_id is None or not message_ids:
        return

    await pending_queue.drop_pending(chat_id, list(message_ids))

    async with SessionLocal() as session:
        orders = (
            await session.execute(
                select(Order).where(
                    Order.source_chat_id == chat_id,
                    Order.source_message_id.in_(message_ids),
                    Order.status.notin_(
                        {OrderStatus.CANCELLED, OrderStatus.AGREED, OrderStatus.EXPIRED}
                    ),
                )
            )
        ).scalars().all()
        if not orders:
            return

        await _cancel_orders(
            session, orders, None, action="cancelled_message_deleted"
        )
        await session.commit()

        log.info(
            "Сообщение удалено в Telegram (chat=%s) — скрыто заказов: %s",
            chat_id,
            [o.id for o in orders],
        )
