"""Публикация заказов в водительские группы: подтверждение у логиста, постинг,
удаление публикаций (вопросы 46-51, 100).

Подтверждение реализовано перепиской в «Избранном» логиста (агент работает от
его же аккаунта, поэтому LOGIST_USER_ID указывает на диалог с самим собой).
Состояние ожидающих подтверждения заказов хранится в памяти процесса — этого
достаточно для одного работающего инстанса агента.
"""

import asyncio
import logging
from datetime import datetime

from sqlalchemy import select
from telethon import TelegramClient
from telethon.errors import RPCError

from app.config import settings
from app.db.base import SessionLocal
from app.models import ActionLog, ActorType, DriverGroup, Order, OrderStatus, Publication
from app.publishing.formatting import format_announcement
from app.publishing.selector import select_groups

log = logging.getLogger("agent.publishing")

# tg_message_id проекта подтверждения -> order_id
_pending_confirmations: dict[int, int] = {}
_last_pending_order_id: int | None = None

# Пауза между отправками в разные группы подряд, чтобы не словить antiflood Telegram.
_POST_DELAY_SECONDS = 2


async def propose_publication(order_id: int, client: TelegramClient) -> None:
    """Показывает логисту предполагаемый список групп и просит подтверждение."""
    global _last_pending_order_id

    async with SessionLocal() as session:
        order = await session.get(Order, order_id)
        if order is None:
            return
        groups = await select_groups(session, order)

    if not groups:
        await client.send_message(
            settings.logist_user_id,
            f"Заказ #{order_id} ({order.from_city} → {order.to_city}): "
            "не нашлось ни одной подходящей водительской группы. Заведите группу под это направление.",
        )
        return

    preview = format_announcement(order)
    group_list = "\n".join(f"  • {g.title}" for g in groups)
    text = (
        f"Заказ #{order_id}, опубликовать в {len(groups)} групп(ы)?\n\n"
        f"{preview}\n\n{group_list}\n\nОтветьте «да» или «нет»."
    )
    msg = await client.send_message(settings.logist_user_id, text)
    _pending_confirmations[msg.id] = order_id
    _last_pending_order_id = order_id


async def handle_confirmation_reply(client: TelegramClient, reply_to_msg_id: int | None, text: str) -> bool:
    """Обрабатывает ответ логиста «да»/«нет». Возвращает True, если ответ был распознан."""
    global _last_pending_order_id

    order_id = None
    if reply_to_msg_id in _pending_confirmations:
        order_id = _pending_confirmations.pop(reply_to_msg_id)
    elif _last_pending_order_id is not None and text in {"да", "нет", "yes", "no"}:
        order_id = _last_pending_order_id

    if order_id is None:
        return False

    _pending_confirmations.pop(reply_to_msg_id, None)
    if _last_pending_order_id == order_id:
        _last_pending_order_id = None

    if text in {"да", "yes"}:
        await publish_order(order_id, client)
    elif text in {"нет", "no"}:
        await client.send_message(settings.logist_user_id, f"Заказ #{order_id}: публикация отменена.")
    else:
        return False
    return True


async def publish_order(order_id: int, client: TelegramClient) -> None:
    """Публикует заказ во все подходящие группы, у которых прошёл антифлуд-таймер."""
    async with SessionLocal() as session:
        order = await session.get(Order, order_id)
        if order is None:
            return
        groups = await select_groups(session, order)
        text = format_announcement(order)
        now = datetime.utcnow()

        posted, skipped = [], []
        for group in groups:
            if group.last_posted_at is not None:
                elapsed = (now - group.last_posted_at).total_seconds()
                if elapsed < group.min_interval_seconds:
                    skipped.append(group.title)
                    continue

            try:
                sent = await client.send_message(group.tg_chat_id, text)
            except RPCError as exc:
                log.warning("Не удалось опубликовать заказ #%s в %s: %s", order_id, group.title, exc)
                continue

            session.add(
                Publication(
                    order_id=order.id,
                    group_id=group.id,
                    tg_message_id=sent.id,
                    text=text,
                    driver_payment_at_post=order.driver_payment,
                    posted_at=now,
                )
            )
            group.last_posted_at = now
            posted.append(group.title)
            await asyncio.sleep(_POST_DELAY_SECONDS)

        if posted and order.status == OrderStatus.NEW:
            order.status = OrderStatus.SEARCHING

        session.add(
            ActionLog(
                order_id=order.id,
                actor=ActorType.AGENT,
                action="published",
                details=f"posted={posted} skipped(antiflood)={skipped}",
            )
        )
        await session.commit()

    summary = f"Заказ #{order_id}: опубликован в {len(posted)} групп(ы)."
    if skipped:
        summary += f" Пропущены (антифлуд-таймер ещё не истёк): {', '.join(skipped)}."
    await client.send_message(settings.logist_user_id, summary)


async def delete_publications(order_id: int, client: TelegramClient, reason: str) -> None:
    """Удаляет все активные публикации заказа (назначение водителя / отмена, вопросы 50-51)."""
    async with SessionLocal() as session:
        order = await session.get(Order, order_id)
        if order is None:
            return

        publications = (
            (
                await session.execute(
                    select(Publication).where(
                        Publication.order_id == order_id, Publication.deleted_at.is_(None)
                    )
                )
            )
            .scalars()
            .all()
        )

        failed_groups = []
        for pub in publications:
            group = await session.get(DriverGroup, pub.group_id)
            try:
                await client.delete_messages(group.tg_chat_id, [pub.tg_message_id])
                pub.deleted_at = datetime.utcnow()
            except RPCError as exc:
                pub.delete_failed = True
                failed_groups.append(group.title)
                log.warning("Не удалось удалить публикацию заказа #%s в %s: %s", order_id, group.title, exc)

        if failed_groups:
            order.has_problem = True
            order.problem_note = f"Не удалось удалить публикацию в: {', '.join(failed_groups)}"
            await client.send_message(
                settings.logist_user_id,
                f"⚠ Заказ #{order_id}: не смог удалить объявление в {', '.join(failed_groups)}. "
                "Удалите вручную.",
            )

        session.add(
            ActionLog(
                order_id=order_id,
                actor=ActorType.AGENT,
                action="publications_deleted",
                details=f"reason={reason} failed={failed_groups}",
            )
        )
        await session.commit()
