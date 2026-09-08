"""Обработка откликов водителей: классификация, торг, сбор данных (вопросы 68-98).

Продолжаем переписку в личке водителя, даже если отклик пришёл в группе
(вопрос 71). Логисту передаём кандидата без ранжирования, просто когда
данные собраны (вопросы 96-98) — решение о назначении остаётся за ним.
"""

import logging
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from telethon import TelegramClient
from telethon.errors import RPCError

from app.config import settings
from app.db.base import SessionLocal
from app.models import (
    ActionLog,
    ActorType,
    Driver,
    DriverGroup,
    DriverResponse,
    Order,
    OrderStatus,
    Publication,
    ResponseKind,
    ResponseStatus,
)
from app.negotiation.classifier import ClassifiedMessage, classify_driver_message
from app.telegram.links import driver_link
from app.workflow.service import handle_assigned_driver_message
from app.negotiation.state import (
    FIELD_PROMPTS,
    UNCLEAR_STREAK_LIMIT,
    bump_unclear_streak,
    get_awaiting,
    mark_processed,
    next_missing_field,
    pop_last_offer,
    reset_unclear_streak,
    set_awaiting,
    set_last_offer,
    unmark_processed,
)

log = logging.getLogger("agent.negotiation")

_CLOSED_STATUSES = {
    OrderStatus.DRIVER_ASSIGNED,
    OrderStatus.IN_PROGRESS,
    OrderStatus.COMPLETED,
    OrderStatus.CANCELLED,
}


async def _safe_send_to_driver(client: TelegramClient, order_id: int, driver_tg_id: int, text: str) -> bool:
    """Не даёт сбою отправки (например PeerFloodError) уронить обработку и потерять отклик."""
    try:
        await client.send_message(driver_tg_id, text)
        return True
    except RPCError as exc:
        log.warning("Не удалось написать водителю %s по заказу #%s: %s", driver_tg_id, order_id, exc)
        try:
            await client.send_message(
                settings.logist_user_id,
                f"⚠ Заказ #{order_id}: не смог написать {driver_link(driver_tg_id)} в личку: {exc}. "
                "Возможно, стоит написать самому.",
                parse_mode="html",
            )
        except RPCError:
            log.exception("Не удалось даже уведомить логиста о сбое отправки.")
        return False


async def _get_or_create_driver(session: AsyncSession, sender) -> Driver:
    driver = (
        await session.execute(select(Driver).where(Driver.tg_user_id == sender.id))
    ).scalar_one_or_none()
    if driver is None:
        driver = Driver(tg_user_id=sender.id, tg_username=getattr(sender, "username", None))
        session.add(driver)
        await session.flush()
    return driver


def _apply_opportunistic_fields(driver: Driver, classified: ClassifiedMessage) -> None:
    """Подхватывает данные авто/имя/телефон из любого сообщения, если они там были."""
    if classified.name and not driver.name:
        driver.name = classified.name
    if classified.phone and not driver.phone:
        driver.phone = classified.phone
    if classified.car_model and not driver.car_model:
        driver.car_model = classified.car_model
    if classified.car_plate and not driver.car_plate:
        driver.car_plate = classified.car_plate


async def handle_group_reply(client: TelegramClient, group_chat_id: int, message) -> None:
    """Водитель ответил в группе реплаем на объявление — заводим отклик и пишем в личку."""
    reply_to = message.reply_to_msg_id
    if not reply_to:
        return
    if not mark_processed(group_chat_id, message.id):
        return  # уже обработали (например повторная доставка события после реконнекта)

    try:
        async with SessionLocal() as session:
            publication = (
                await session.execute(
                    select(Publication)
                    .join(DriverGroup)
                    .where(
                        DriverGroup.tg_chat_id == group_chat_id,
                        Publication.tg_message_id == reply_to,
                        Publication.deleted_at.is_(None),
                    )
                )
            ).scalar_one_or_none()
            if publication is None:
                return

            order = await session.get(Order, publication.order_id)
            if order is None or order.status in _CLOSED_STATUSES:
                return

            sender = await message.get_sender()
            driver = await _get_or_create_driver(session, sender)

            text = (message.raw_text or "").strip()
            classified = await classify_driver_message(text) if text else ClassifiedMessage(kind=ResponseKind.UNCLEAR)
            _apply_opportunistic_fields(driver, classified)

            # Если у водителя уже есть открытый отклик по этому же заказу (например,
            # ответил "+", а потом отдельным сообщением уточнил сумму) — обновляем
            # его, а не заводим второй ряд (иначе /assign увидит "двух" кандидатов
            # в одном и том же человеке).
            existing_response = (
                await session.execute(
                    select(DriverResponse).where(
                        DriverResponse.order_id == order.id,
                        DriverResponse.driver_id == driver.id,
                        DriverResponse.status.in_(
                            [ResponseStatus.NEW, ResponseStatus.NEGOTIATING, ResponseStatus.FORWARDED]
                        ),
                    )
                )
            ).scalar_one_or_none()

            is_first_contact = existing_response is None
            if existing_response is not None:
                response = existing_response
                response.raw_text = text
                response.kind = classified.kind
                if classified.requested_payment is not None:
                    response.requested_payment = classified.requested_payment
            else:
                response = DriverResponse(
                    order_id=order.id,
                    driver_id=driver.id,
                    raw_text=text,
                    kind=classified.kind,
                    requested_payment=classified.requested_payment,
                )
                session.add(response)

            if order.status in (OrderStatus.NEW, OrderStatus.SEARCHING):
                order.status = OrderStatus.HAS_RESPONSES
            await session.flush()

            if is_first_contact:
                await _safe_send_to_driver(
                    client, order.id, driver.tg_user_id, "Здравствуйте! Пишу по вашему отклику на заказ в группе."
                )
            await _react_to_kind(client, session, driver, response, order, classified)
            await session.commit()
    except Exception:
        unmark_processed(group_chat_id, message.id)
        raise


async def handle_driver_dm(client: TelegramClient, sender_id: int, message) -> None:
    """Личное сообщение от водителя — продолжение переговоров по последнему открытому отклику."""
    if not mark_processed(message.chat_id, message.id):
        return  # уже обработали (например повторная доставка события после реконнекта)

    try:
        await _handle_driver_dm_inner(client, sender_id, message)
    except Exception:
        unmark_processed(message.chat_id, message.id)
        raise


async def _find_order_by_route_hint(session: AsyncSession, text: str) -> Order | None:
    """Водитель написал в личку 'с нуля', не отвечая на объявление в группе.

    Пытаемся угадать заказ по упомянутым городам маршрута среди ещё активных
    заявок. Если совпадений нет или их несколько — лучше переспросить, чем
    угадать неправильно."""
    text_lower = text.lower()
    candidates = (
        await session.execute(
            select(Order).where(Order.status.in_([OrderStatus.NEW, OrderStatus.SEARCHING, OrderStatus.HAS_RESPONSES]))
        )
    ).scalars().all()
    matches = [
        o
        for o in candidates
        if o.from_city and o.to_city and o.from_city.lower() in text_lower and o.to_city.lower() in text_lower
    ]
    return matches[0] if len(matches) == 1 else None


async def _handle_driver_dm_inner(client: TelegramClient, sender_id: int, message) -> None:
    async with SessionLocal() as session:
        driver = (
            await session.execute(select(Driver).where(Driver.tg_user_id == sender_id))
        ).scalar_one_or_none()
        if driver is None:
            # Незнакомый контакт написал в личку сам, без ответа в группе —
            # заводим карточку водителя, дальше разберёмся по тексту сообщения.
            sender = await message.get_sender()
            driver = await _get_or_create_driver(session, sender)

        # Приоритет — уже назначенные заказы: "созвонился"/"оплатил" не должны попасть
        # в переговоры по какому-то ДРУГОМУ заказу, даже если те ещё открыты.
        text = (message.raw_text or "").strip()
        if text:
            assigned_responses = (
                await session.execute(
                    select(DriverResponse)
                    .where(DriverResponse.driver_id == driver.id, DriverResponse.status == ResponseStatus.ASSIGNED)
                    .order_by(DriverResponse.id.desc())
                )
            ).scalars().all()
            for assigned_response in assigned_responses:
                assigned_order = await session.get(Order, assigned_response.order_id)
                if assigned_order is None:
                    continue
                handled = await handle_assigned_driver_message(client, session, driver, assigned_order, text)
                if handled:
                    await session.commit()
                    return

        # FORWARDED тоже считаем «открытым»: водитель мог задать ещё вопрос уже
        # после того, как его данные ушли логисту, и это не должно теряться.
        response = (
            await session.execute(
                select(DriverResponse)
                .where(
                    DriverResponse.driver_id == driver.id,
                    DriverResponse.status.in_(
                        [ResponseStatus.NEW, ResponseStatus.NEGOTIATING, ResponseStatus.FORWARDED]
                    ),
                )
                .order_by(DriverResponse.id.desc())
            )
        ).scalars().first()
        if response is None:
            if not text:
                return  # фото/стикер без единого слова текста — зацепиться не за что

            hinted_order = await _find_order_by_route_hint(session, text)
            if hinted_order is None:
                await _safe_send_to_driver(
                    client,
                    0,
                    driver.tg_user_id,
                    "Здравствуйте! По какому заказу вы пишете — уточните маршрут, "
                    "или ответьте прямо на объявление в группе, так я не перепутаю.",
                )
                await session.commit()
                return

            classified = await classify_driver_message(text)
            _apply_opportunistic_fields(driver, classified)
            response = DriverResponse(
                order_id=hinted_order.id,
                driver_id=driver.id,
                raw_text=text,
                kind=classified.kind,
                requested_payment=classified.requested_payment,
            )
            session.add(response)
            if hinted_order.status in (OrderStatus.NEW, OrderStatus.SEARCHING):
                hinted_order.status = OrderStatus.HAS_RESPONSES
            await session.flush()

            await _react_to_kind(client, session, driver, response, hinted_order, classified)
            await session.commit()
            return

        order = await session.get(Order, response.order_id)

        awaiting = get_awaiting(driver.tg_user_id)
        if awaiting and awaiting.startswith("photo_"):
            if message.photo:
                # Файлы не выгружаем, храним ссылку на сообщение в Telegram (вопрос 86).
                setattr(driver, awaiting, f"{message.chat_id}:{message.id}")
                set_awaiting(driver.tg_user_id, None)
                await _advance(client, session, driver, response, order)
            else:
                await _safe_send_to_driver(
                    client, order.id, driver.tg_user_id, f"Нужно именно фото. {FIELD_PROMPTS[awaiting]}"
                )
            await session.commit()
            return

        if not text:
            return

        if awaiting:
            if _looks_like_valid_answer(awaiting, text):
                setattr(driver, awaiting, text)
                set_awaiting(driver.tg_user_id, None)
                await _advance(client, session, driver, response, order)
            else:
                await _safe_send_to_driver(
                    client,
                    order.id,
                    driver.tg_user_id,
                    f"Не понял ответ. {FIELD_PROMPTS[awaiting]}",
                )
            await session.commit()
            return

        if response.status == ResponseStatus.FORWARDED:
            # Уже передали логисту — не переклассифицируем и не дублируем карточку,
            # просто подтверждаем, что сообщение дошло.
            await _safe_send_to_driver(client, order.id, driver.tg_user_id, "Данные уже переданы логисту, ждите.")
            await session.commit()
            return

        classified = await classify_driver_message(text)
        _apply_opportunistic_fields(driver, classified)
        response.raw_text = text
        response.kind = classified.kind
        if classified.requested_payment is not None:
            response.requested_payment = classified.requested_payment

        await _react_to_kind(client, session, driver, response, order, classified)
        await session.commit()


def _looks_like_valid_answer(field: str, text: str) -> bool:
    """Грубый фильтр от «Не разобрал» ответов на прямой вопрос при сборе данных водителя.

    Не строгая валидация формата — только отсекает явные не-ответы (встречный
    вопрос, слишком короткая реплика), чтобы не записать их в карточку водителя.
    """
    if "?" in text:
        return False
    if field == "phone":
        return sum(ch.isdigit() for ch in text) >= 5
    return len(text) >= 2


async def _react_to_kind(
    client: TelegramClient,
    session: AsyncSession,
    driver: Driver,
    response: DriverResponse,
    order: Order,
    classified: ClassifiedMessage,
) -> None:
    driver_tg_id = driver.tg_user_id

    if order is None or order.status in _CLOSED_STATUSES:
        await _safe_send_to_driver(client, order.id if order else 0, driver_tg_id, "Этот заказ уже разобрали, спасибо за отклик!")
        response.status = ResponseStatus.REJECTED
        return

    kind = classified.kind
    order_brief = (
        f"{order.from_city} → {order.to_city}, "
        f"{order.pickup_at.strftime('%d.%m %H:%M') if order.pickup_at else 'время уточняется'}, "
        f"оплата {order.driver_payment or order.client_price or '?'} ₽"
    )

    if kind == ResponseKind.QUESTION:
        reset_unclear_streak(driver_tg_id)
        await _safe_send_to_driver(client, order.id, driver_tg_id, f"Да, ещё актуально: {order_brief}.")
        return
    if kind == ResponseKind.HOLD:
        reset_unclear_streak(driver_tg_id)
        await _safe_send_to_driver(client, order.id, driver_tg_id, "Хорошо, подождём вашего решения.")
        return
    if kind == ResponseKind.UNCLEAR:
        streak = bump_unclear_streak(driver_tg_id)
        if streak > UNCLEAR_STREAK_LIMIT:
            reset_unclear_streak(driver_tg_id)
            await _safe_send_to_driver(
                client, order.id, driver_tg_id, "Передал ваш вопрос логисту, он свяжется с вами сам."
            )
            session.add(
                ActionLog(
                    order_id=order.id,
                    actor=ActorType.AGENT,
                    action="response_escalated",
                    details=f"driver_id={driver.id} raw_text={response.raw_text!r}",
                )
            )
            await client.send_message(
                settings.logist_user_id,
                f"⚠ Заказ #{order.id}: не могу понять {driver_link(driver_tg_id, driver.name)}, напишите сами.\n"
                f"Последнее сообщение: {response.raw_text!r}",
                parse_mode="html",
            )
            return
        await _safe_send_to_driver(
            client,
            order.id,
            driver_tg_id,
            f"Уточните, пожалуйста — подходит вам заказ {order_brief}?",
        )
        return

    reset_unclear_streak(driver_tg_id)
    if kind == ResponseKind.BARGAIN:
        if classified.requested_payment is None:
            # «Торг есть?» без суммы — не тупик, а прямой вопрос к водителю.
            await _safe_send_to_driver(client, order.id, driver_tg_id, "За какую сумму вы согласны? Напишите цифру в рублях.")
            return

        base = order.driver_payment or order.client_price
        if not base:
            # Цену никто не назвал (LLM не разобрал стоимость заявки) — торговаться не от чего,
            # это на усмотрение логиста, а не автоторга.
            await _safe_send_to_driver(client, order.id, driver_tg_id, "Секунду, уточняю у логиста.")
            await client.send_message(
                settings.logist_user_id,
                f"⚠ Заказ #{order.id}: у заявки не задана оплата водителю, а "
                f"{driver_link(driver_tg_id, driver.name)} просит {classified.requested_payment} ₽. "
                "Договоритесь сами и назначьте через /assign.",
                parse_mode="html",
            )
            return

        max_allowed = base * (1 + Decimal(settings.max_negotiation_uplift_pct) / 100)
        if classified.requested_payment <= max_allowed:
            response.agreed_payment = classified.requested_payment
            response.status = ResponseStatus.NEGOTIATING
            pop_last_offer(driver_tg_id)
            await _safe_send_to_driver(client, order.id, driver_tg_id, f"Договорились, {classified.requested_payment:.0f} ₽.")
        else:
            set_last_offer(driver_tg_id, max_allowed)
            await _safe_send_to_driver(client, order.id, driver_tg_id, f"Больше {max_allowed:.0f} ₽ дать не могу, устроит?")
            return  # ждём решения водителя, данные пока не запрашиваем
    elif kind == ResponseKind.ACCEPT:
        # Если до этого предлагали компромиссную сумму — соглашается именно на неё,
        # а не откатывается на исходную оплату по заказу.
        response.agreed_payment = response.agreed_payment or pop_last_offer(driver_tg_id) or order.driver_payment
        response.status = ResponseStatus.NEGOTIATING
        await _safe_send_to_driver(client, order.id, driver_tg_id, "Отлично!")
    else:
        return

    await _advance(client, session, driver, response, order)


async def _advance(
    client: TelegramClient, session: AsyncSession, driver: Driver, response: DriverResponse, order: Order
) -> None:
    """Запрашивает следующее недостающее поле или передаёт кандидата логисту."""
    missing = next_missing_field(driver)
    if missing:
        set_awaiting(driver.tg_user_id, missing)
        await _safe_send_to_driver(client, order.id, driver.tg_user_id, FIELD_PROMPTS[missing])
        return

    response.status = ResponseStatus.FORWARDED
    if order.status not in _CLOSED_STATUSES:
        order.status = OrderStatus.HAS_RESPONSES

    count = (
        await session.execute(
            select(func.count()).select_from(DriverResponse).where(DriverResponse.order_id == order.id)
        )
    ).scalar_one()

    session.add(
        ActionLog(
            order_id=order.id,
            actor=ActorType.AGENT,
            action="response_forwarded",
            details=f"driver_id={driver.id}",
        )
    )

    summary = (
        f"Заказ #{order.id} ({order.from_city} → {order.to_city}): кандидат готов.\n"
        f"Водитель: {driver_link(driver.tg_user_id, driver.name)}, {driver.phone}\n"
        f"Авто: {driver.car_model} {driver.car_plate}\n"
        f"Оплата: {response.agreed_payment or order.driver_payment} ₽\n"
        f"Откликов на заказ: {count}"
    )
    await client.send_message(settings.logist_user_id, summary, parse_mode="html")
    await _safe_send_to_driver(
        client, order.id, driver.tg_user_id, "Спасибо! Передал данные логисту, ждите подтверждения."
    )
