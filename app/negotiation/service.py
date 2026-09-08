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
    reset_unclear_streak,
    set_awaiting,
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

        await _safe_send_to_driver(
            client, order.id, driver.tg_user_id, "Здравствуйте! Пишу по вашему отклику на заказ в группе."
        )
        await _react_to_kind(client, session, driver, response, order, classified)
        await session.commit()


async def handle_driver_dm(client: TelegramClient, sender_id: int, message) -> None:
    """Личное сообщение от водителя — продолжение переговоров по последнему открытому отклику."""
    if not mark_processed(message.chat_id, message.id):
        return  # уже обработали (например повторная доставка события после реконнекта)

    async with SessionLocal() as session:
        driver = (
            await session.execute(select(Driver).where(Driver.tg_user_id == sender_id))
        ).scalar_one_or_none()
        if driver is None:
            return  # неизвестный контакт — вне зоны действия MVP, разберёт логист вручную

        # Приоритет — уже назначенные заказы: "созвонился"/"оплатил" не должны попасть
        # в переговоры по какому-то ДРУГОМУ заказу, даже если те ещё открыты.
        text = (message.raw_text or "").strip()
        if text:
            assigned_responses = (
                await session.execute(
                    select(DriverResponse)
                    .where(DriverResponse.driver_id == driver.id, DriverResponse.status == ResponseStatus.ASSIGNED)
                    .order_by(DriverResponse.created_at.desc())
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

        response = (
            await session.execute(
                select(DriverResponse)
                .where(
                    DriverResponse.driver_id == driver.id,
                    DriverResponse.status.in_([ResponseStatus.NEW, ResponseStatus.NEGOTIATING]),
                )
                .order_by(DriverResponse.created_at.desc())
            )
        ).scalars().first()
        if response is None:
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

        text = (message.raw_text or "").strip()
        if not text:
            return

        if awaiting:
            setattr(driver, awaiting, text)
            set_awaiting(driver.tg_user_id, None)
            await _advance(client, session, driver, response, order)
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
    if kind == ResponseKind.BARGAIN and classified.requested_payment is not None:
        base = order.driver_payment or Decimal(0)
        max_allowed = base * (1 + Decimal(settings.max_negotiation_uplift_pct) / 100)
        if classified.requested_payment <= max_allowed:
            response.agreed_payment = classified.requested_payment
            response.status = ResponseStatus.NEGOTIATING
            await _safe_send_to_driver(client, order.id, driver_tg_id, f"Договорились, {classified.requested_payment:.0f} ₽.")
        else:
            await _safe_send_to_driver(client, order.id, driver_tg_id, f"Больше {max_allowed:.0f} ₽ дать не могу, устроит?")
            return  # ждём решения водителя, данные пока не запрашиваем
    elif kind == ResponseKind.ACCEPT:
        response.agreed_payment = response.agreed_payment or order.driver_payment
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
