"""Назначение водителя и завершение заказа — решения с человеческим весом
остаются за логистом, агент только исполняет (вопросы 35-37, 104-113, 122).
"""

import logging
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from telethon import TelegramClient
from telethon.errors import RPCError

from app.config import settings
from app.db.base import SessionLocal
from app.models import ActionLog, ActorType, Driver, DriverResponse, Order, OrderStatus, ResponseStatus
from app.publishing.service import delete_publications
from app.telegram.links import driver_link

log = logging.getLogger("agent.workflow")

# Узкие, конкретные фразы — короткие слова вроде "подтверд" или "на связи" ловили
# посторонние сообщения ("подтвердите адрес подачи") и ошибочно переключали статус.
_CALL_CONFIRMED_KEYWORDS = ("созвонил", "дозвонил", "клиент подтвердил", "договорились с клиентом")
_PAYMENT_CLAIM_KEYWORDS = (
    "оплатил комиссию",
    "перевел комиссию",
    "перевёл комиссию",
    "скинул комиссию",
    "комиссия оплачена",
    "комиссию отправил",
    "комиссию перекинул",
)


async def _safe_send(client: TelegramClient, tg_id: int, text: str) -> bool:
    try:
        await client.send_message(tg_id, text)
        return True
    except RPCError as exc:
        log.warning("Не удалось отправить сообщение %s: %s", tg_id, exc)
        return False


def _order_brief(order: Order) -> str:
    return f"#{order.id} ({order.from_city} → {order.to_city})"


def _driver_details_for_client(order: Order) -> str:
    lines = [
        f"Заказ {_order_brief(order)} назначен вам!",
        f"Подача: {order.pickup_at.strftime('%d.%m %H:%M') if order.pickup_at else 'уточняется'}",
        f"Откуда: {order.from_address or order.from_city}",
        f"Куда: {order.to_address or order.to_city}",
    ]
    if order.flight_or_train:
        lines.append(f"Рейс/поезд: {order.flight_or_train}")
    if order.passengers:
        lines.append(f"Пассажиров: {order.passengers}")

    if order.client_name or order.client_phone:
        client_bits = [v for v in (order.client_name, order.client_phone) if v]
        lines.append(f"Клиент: {', '.join(client_bits)}")
        lines.append("Позвоните клиенту, подтвердите время подачи и напишите мне «созвонился».")
    else:
        # Диспетчер не указал контакт клиента — не подсовываем водителю "?, ?",
        # честно говорим, что уточняется, и не просим звонить в никуда.
        lines.append("Контакт клиента уточняется у диспетчера, скоро пришлю.")
        lines.append("Как подтвердите созвон с клиентом — напишите мне «созвонился».")
    return "\n".join(lines)


async def assign_driver(client: TelegramClient, order_id: int, response_id: int | None) -> str:
    """Назначает водителя на заказ (команда логиста /assign). Возвращает текст ответа логисту."""
    async with SessionLocal() as session:
        order = await session.get(Order, order_id)
        if order is None:
            return f"Заказ #{order_id} не найден."
        if order.status in (OrderStatus.DRIVER_ASSIGNED, OrderStatus.IN_PROGRESS, OrderStatus.COMPLETED):
            return f"Заказ #{order_id} уже в статусе «{order.status.value}»."

        query = select(DriverResponse).where(
            DriverResponse.order_id == order_id, DriverResponse.status == ResponseStatus.FORWARDED
        )
        if response_id is not None:
            query = query.where(DriverResponse.id == response_id)
        candidates = (await session.execute(query)).scalars().all()

        if not candidates:
            return f"Нет переданных кандидатов для заказа #{order_id}. Дождитесь отклика или укажите response_id."
        if len(candidates) > 1:
            lines = [f"По заказу #{order_id} несколько кандидатов, уточните: /assign {order_id} <response_id>"]
            for r in candidates:
                driver = await session.get(Driver, r.driver_id)
                lines.append(f"  #{r.id}: {driver.name or driver.tg_username}")
            return "\n".join(lines)

        response = candidates[0]
        driver = await session.get(Driver, response.driver_id)

        order.assigned_driver_id = driver.id
        order.assigned_at = datetime.utcnow()
        order.status = OrderStatus.DRIVER_ASSIGNED
        response.status = ResponseStatus.ASSIGNED

        others = (
            await session.execute(
                select(DriverResponse).where(
                    DriverResponse.order_id == order_id,
                    DriverResponse.id != response.id,
                    DriverResponse.status.in_(
                        [ResponseStatus.NEW, ResponseStatus.NEGOTIATING, ResponseStatus.FORWARDED]
                    ),
                )
            )
        ).scalars().all()
        for other in others:
            other.status = ResponseStatus.REJECTED

        session.add(
            ActionLog(
                order_id=order.id,
                actor=ActorType.LOGIST,
                actor_tg_id=settings.logist_user_id,
                action="driver_assigned",
                details=f"driver_id={driver.id}",
            )
        )
        await session.commit()

        declined_tg_ids = []
        for other in others:
            other_driver = await session.get(Driver, other.driver_id)
            if other_driver.id != driver.id:
                declined_tg_ids.append(other_driver.tg_user_id)

    try:
        await delete_publications(order_id, client, reason="assigned")
    except Exception:
        log.exception("Не удалось удалить публикации заказа #%s при назначении", order_id)

    sent_ok = await _safe_send(client, driver.tg_user_id, _driver_details_for_client(order))
    for tg_id in declined_tg_ids:
        await _safe_send(client, tg_id, f"Заказ {_order_brief(order)} уже отдали другому водителю, спасибо за отклик!")

    result = f"Заказ {_order_brief(order)} назначен {driver.name or driver.tg_username}."
    result += " Отправил данные водителю." if sent_ok else " ⚠ Не смог написать водителю — свяжитесь сами."
    if not (order.client_name or order.client_phone):
        result += " ⚠ У заявки нет контакта клиента — впишите его в веб-панели, иначе водителю звонить некуда."
    return result


async def complete_order(client: TelegramClient, order_id: int) -> str:
    async with SessionLocal() as session:
        order = await session.get(Order, order_id)
        if order is None:
            return f"Заказ #{order_id} не найден."
        order.status = OrderStatus.COMPLETED
        session.add(
            ActionLog(order_id=order.id, actor=ActorType.LOGIST, action="order_completed")
        )
        await session.commit()
    return f"Заказ {_order_brief(order)} завершён."


async def cancel_order(client: TelegramClient, order_id: int) -> str:
    async with SessionLocal() as session:
        order = await session.get(Order, order_id)
        if order is None:
            return f"Заказ #{order_id} не найден."

        assigned_driver = None
        if order.assigned_driver_id is not None:
            assigned_response = (
                await session.execute(
                    select(DriverResponse).where(
                        DriverResponse.order_id == order_id, DriverResponse.status == ResponseStatus.ASSIGNED
                    )
                )
            ).scalar_one_or_none()
            if assigned_response is not None:
                assigned_response.status = ResponseStatus.REJECTED
            assigned_driver = await session.get(Driver, order.assigned_driver_id)

        order.status = OrderStatus.CANCELLED
        session.add(ActionLog(order_id=order.id, actor=ActorType.LOGIST, action="order_cancelled"))
        await session.commit()

    try:
        await delete_publications(order_id, client, reason="cancelled")
    except Exception:
        log.exception("Не удалось удалить публикации заказа #%s при отмене", order_id)

    if assigned_driver is not None:
        await _safe_send(
            client, assigned_driver.tg_user_id, f"Заказ {_order_brief(order)} отменён, извините за неудобства."
        )

    return f"Заказ {_order_brief(order)} отменён, публикации удалены."


async def confirm_payment(client: TelegramClient, order_id: int) -> str:
    async with SessionLocal() as session:
        order = await session.get(Order, order_id)
        if order is None:
            return f"Заказ #{order_id} не найден."
        order.commission_paid = True
        session.add(ActionLog(order_id=order.id, actor=ActorType.LOGIST, action="payment_confirmed"))
        driver = await session.get(Driver, order.assigned_driver_id) if order.assigned_driver_id else None
        await session.commit()
    if driver is not None:
        await _safe_send(client, driver.tg_user_id, f"Спасибо! Оплата по заказу {_order_brief(order)} подтверждена.")
    return f"Оплата по заказу {_order_brief(order)} подтверждена."


def _looks_like(text: str, keywords: tuple[str, ...]) -> bool:
    lowered = text.lower()
    return any(word in lowered for word in keywords)


async def handle_assigned_driver_message(
    client: TelegramClient, session: AsyncSession, driver: Driver, order: Order, text: str
) -> bool:
    """Сообщения уже назначенного водителя: подтверждение созвона, заявление об оплате."""
    if order.status == OrderStatus.DRIVER_ASSIGNED and _looks_like(text, _CALL_CONFIRMED_KEYWORDS):
        order.status = OrderStatus.IN_PROGRESS
        order.driver_confirmed_call_at = datetime.utcnow()
        session.add(
            ActionLog(
                order_id=order.id, actor=ActorType.DRIVER, actor_tg_id=driver.tg_user_id, action="call_confirmed"
            )
        )

        payment_msg = "Отлично! Заказ переведён в работу."
        if settings.payment_details:
            payment_msg += f"\nПосле поездки переведите комиссию по реквизитам:\n{settings.payment_details}"
        await _safe_send(client, driver.tg_user_id, payment_msg)

        await client.send_message(
            settings.logist_user_id,
            f"Заказ {_order_brief(order)}: водитель {driver_link(driver.tg_user_id, driver.name)} "
            "подтвердил созвон с клиентом, статус — «в работе».",
            parse_mode="html",
        )
        return True

    if order.status == OrderStatus.IN_PROGRESS and not order.commission_paid and _looks_like(text, _PAYMENT_CLAIM_KEYWORDS):
        order.commission_screenshot_at = datetime.utcnow()
        session.add(
            ActionLog(
                order_id=order.id, actor=ActorType.DRIVER, actor_tg_id=driver.tg_user_id, action="payment_claimed"
            )
        )
        await client.send_message(
            settings.logist_user_id,
            f"Заказ {_order_brief(order)}: водитель {driver_link(driver.tg_user_id, driver.name)} говорит, что "
            f"оплатил комиссию. Проверьте и подтвердите: /paid {order.id}",
            parse_mode="html",
        )
        await _safe_send(client, driver.tg_user_id, "Спасибо, проверяю.")
        return True

    return False
