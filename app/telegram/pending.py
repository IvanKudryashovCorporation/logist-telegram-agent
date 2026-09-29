"""Очередь повторного разбора сообщений (``pending_messages``).

Telethon НЕ переигрывает уже доставленное событие. Если LLM ответила
таймаутом/429/5xx, сообщение раньше терялось навсегда: обработчик ронял
исключение, заявка не попадала в БД, и узнать об этом можно было только
вручную сверяя лог с группой. Теперь такое сообщение ставится в очередь, а
воркер (:mod:`app.telegram.queue_worker`) повторяет разбор с экспоненциальной
задержкой, пока не получится или не кончатся попытки.

Повторная постановка того же сообщения идемпотентна (unique на chat+message):
счётчик попыток продолжает расти, а не сбрасывается.
"""

import logging
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.base import SessionLocal
from app.models import PendingMessage, PendingStatus
from app.timeutil import now_utc_naive

log = logging.getLogger("agent.queue")


def next_attempt_at(attempts: int, *, now: Optional[datetime] = None) -> datetime:
    """Когда пробовать в следующий раз: база * 2^attempts, с потолком в 6 часов.

    ``attempts`` — число УЖЕ сделанных попыток, поэтому первая отсрочка равна
    ``queue_base_delay_seconds``.
    """
    base = max(1, settings.queue_base_delay_seconds)
    delay = min(base * (2 ** max(0, attempts - 1)), 6 * 3600)
    return (now or now_utc_naive()) + timedelta(seconds=delay)


async def enqueue(
    *,
    chat_id: int,
    message_id: int,
    text: str,
    is_edit: bool = False,
    error: Optional[str] = None,
    session: Optional[AsyncSession] = None,
) -> PendingMessage:
    """Ставит сообщение в очередь (или обновляет существующую запись).

    Коммитит сам, если сессия не передана: постановка в очередь обязана
    пережить падение той транзакции, в которой разбор не удался.
    """
    own_session = session is None
    db = session or SessionLocal()
    try:
        pending = (
            await db.execute(
                select(PendingMessage).where(
                    PendingMessage.chat_id == chat_id,
                    PendingMessage.message_id == message_id,
                )
            )
        ).scalar_one_or_none()

        if pending is None:
            pending = PendingMessage(
                chat_id=chat_id,
                message_id=message_id,
                text=text or "",
                is_edit=is_edit,
                status=PendingStatus.PENDING,
                attempts=0,
            )
            db.add(pending)
        else:
            # Сообщение уже ждало разбора — освежаем текст и возвращаем в работу,
            # даже если прошлый цикл закончился статусом FAILED.
            pending.text = text or pending.text
            pending.is_edit = is_edit or pending.is_edit
            pending.status = PendingStatus.PENDING
            pending.last_error = error

        pending.next_attempt_at = next_attempt_at(pending.attempts + 1)
        pending.last_error = error
        if own_session:
            await db.commit()
        else:
            await db.flush()
        log.warning(
            "Сообщение chat=%s msg=%s отложено в очередь (попытка %s, причина: %s)",
            chat_id, message_id, pending.attempts + 1, error,
        )
        return pending
    finally:
        if own_session:
            await db.close()


async def fetch_due(session: AsyncSession, *, limit: int = 10) -> list[PendingMessage]:
    """Сообщения, у которых подошёл срок повторной попытки."""
    now = now_utc_naive()
    rows = await session.execute(
        select(PendingMessage)
        .where(
            PendingMessage.status == PendingStatus.PENDING,
            func.coalesce(PendingMessage.next_attempt_at, now) <= now,
        )
        .order_by(PendingMessage.next_attempt_at.asc(), PendingMessage.id.asc())
        .limit(limit)
    )
    return list(rows.scalars().all())


async def mark_done(session: AsyncSession, pending: PendingMessage) -> None:
    pending.status = PendingStatus.DONE
    pending.last_error = None
    session.add(pending)


async def mark_retry(session: AsyncSession, pending: PendingMessage, error: str) -> None:
    """Попытка не удалась: сдвигаем срок или сдаёмся, если лимит исчерпан."""
    pending.attempts += 1
    pending.last_error = error[:2000]
    if pending.attempts >= max(1, settings.queue_max_attempts):
        pending.status = PendingStatus.FAILED
        log.error(
            "Сообщение chat=%s msg=%s не разобрано после %s попыток — нужен человек "
            "(scripts/retry_queue.py --reset)",
            pending.chat_id, pending.message_id, pending.attempts,
        )
    else:
        pending.next_attempt_at = next_attempt_at(pending.attempts + 1)
        log.warning(
            "Повтор разбора chat=%s msg=%s не удался (%s/%s): %s",
            pending.chat_id, pending.message_id, pending.attempts,
            settings.queue_max_attempts, error,
        )
    session.add(pending)


async def drop_pending(chat_id: int, message_ids: list[int]) -> int:
    """Убирает из очереди сообщения, которые больше не нужно разбирать.

    Вызывается, когда диспетчер удалил сообщение в Telegram: повторять разбор
    несуществующего сообщения бессмысленно.
    """
    if not message_ids:
        return 0
    async with SessionLocal() as session:
        rows = await session.execute(
            select(PendingMessage).where(
                PendingMessage.chat_id == chat_id,
                PendingMessage.message_id.in_(message_ids),
                PendingMessage.status == PendingStatus.PENDING,
            )
        )
        pending = list(rows.scalars().all())
        for item in pending:
            item.status = PendingStatus.DONE
            item.last_error = "message_deleted"
            session.add(item)
        await session.commit()
        return len(pending)


async def queue_depth() -> dict[str, int]:
    """Сколько сообщений в каждом состоянии — для /admin и логов.

    Все статусы присутствуют в словаре всегда, даже с нулём: вызывающий код
    (админка, отчёты) обращается к ключам напрямую и не должен падать с
    KeyError на пустой очереди.
    """
    async with SessionLocal() as session:
        rows = await session.execute(
            select(PendingMessage.status, func.count(PendingMessage.id)).group_by(
                PendingMessage.status
            )
        )
        counts = rows.all()
    depth = {status.value: 0 for status in PendingStatus}
    for status, count in counts:
        key = status.value if isinstance(status, PendingStatus) else str(status)
        depth[key] = count
    return depth


async def failed_messages(limit: int = 50) -> list[PendingMessage]:
    """Сообщения, по которым попытки исчерпаны, — список для админки."""
    async with SessionLocal() as session:
        rows = await session.execute(
            select(PendingMessage)
            .where(PendingMessage.status == PendingStatus.FAILED)
            .order_by(PendingMessage.id.desc())
            .limit(limit)
        )
        return list(rows.scalars().all())
