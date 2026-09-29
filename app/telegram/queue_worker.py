"""Фоновый воркер очереди повторного разбора (``pending_messages``).

Сообщение попадает в очередь, когда LLM/сеть отказали. Воркер периодически
достаёт те, у которых подошёл срок, ЗАНОВО читает сообщение из Telegram
(чтобы учесть правки диспетчера за время ожидания) и прогоняет через то же
ядро разбора, что и обычные события.

Идемпотентность: если разбор удался, а пометку «готово» записать не удалось,
сообщение просто разберётся ещё раз — дубль заказа не создастся thanks to
``uq_orders_source``.
"""

import asyncio
import logging
from typing import Optional

from telethon import TelegramClient

from app.config import settings
from app.db.base import SessionLocal
from app.models import ParseOutcome, PendingMessage, PendingStatus
from app.parsing import stats as parse_stats
from app.parsing.llm_parser import text_hash
from app.services.notify import notifier
from app.telegram import pending as pending_queue
from app.telegram.work_group import upsert_order_text

log = logging.getLogger("agent.queue_worker")


async def _load(session, pending_id: int) -> Optional[PendingMessage]:
    return await session.get(PendingMessage, pending_id)


async def process_one(client: TelegramClient, pending_id: int) -> bool:
    """Обрабатывает одну запись очереди. True — разбор успешно завершён."""
    async with SessionLocal() as session:
        pending = await _load(session, pending_id)
        if pending is None or pending.status != PendingStatus.PENDING:
            return True

        chat_id, message_id, is_edit = pending.chat_id, pending.message_id, pending.is_edit
        stored_text = pending.text

    try:
        message = await client.get_messages(chat_id, ids=message_id)
    except Exception as exc:  # noqa: BLE001 — сеть/лимиты Telegram
        await _finish(pending_id, ok=False, error=f"get_messages: {type(exc).__name__}: {exc}")
        return False

    if message is None:
        # Диспетчер удалил сообщение — разбирать больше нечего.
        await _finish(pending_id, ok=True, note="message_gone")
        return True

    text = (getattr(message, "raw_text", "") or stored_text or "").strip()
    sender = await message.get_sender()

    try:
        order_ids = await upsert_order_text(
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            dispatcher_tg_id=getattr(sender, "id", None),
            dispatcher_username=getattr(sender, "username", None),
            is_edit=is_edit,
        )
    except Exception as exc:  # noqa: BLE001
        detail = f"{type(exc).__name__}: {exc}"
        await _finish(pending_id, ok=False, error=detail)
        await notifier.note_parse_error(detail)
        return False

    await notifier.note_parse_success()
    log.info(
        "Очередь: сообщение chat=%s msg=%s разобрано, заказов=%s",
        chat_id, message_id, len(order_ids),
    )
    await _finish(pending_id, ok=True)
    return True


async def _finish(pending_id: int, *, ok: bool, error: Optional[str] = None,
                  note: Optional[str] = None) -> None:
    """Обновляет состояние записи очереди в своей транзакции."""
    async with SessionLocal() as session:
        pending = await _load(session, pending_id)
        if pending is None:
            return
        if ok:
            await pending_queue.mark_done(session, pending)
            if note:
                pending.last_error = note
        else:
            await pending_queue.mark_retry(session, pending, error or "unknown")
            await parse_stats.record(
                session,
                ParseOutcome.ERROR,
                chat_id=pending.chat_id,
                message_id=pending.message_id,
                text_hash=text_hash(pending.text),
                error=error,
            )
        await session.commit()
        if pending.status == PendingStatus.FAILED:
            await notifier.send(
                f"⚠️ Агрегатор: сообщение chat={pending.chat_id} msg={pending.message_id} "
                f"не разобрано после {pending.attempts} попыток.\n"
                f"Причина: {(pending.last_error or '')[:300]}\n"
                f"Текст: {pending.text[:300]}"
            )


async def process_due_once(client: TelegramClient, *, limit: int = 10) -> int:
    """Один проход по сообщениям с наступившим сроком. Возвращает число обработанных."""
    async with SessionLocal() as session:
        due = await pending_queue.fetch_due(session, limit=limit)
        due_ids = [item.id for item in due]
    for pending_id in due_ids:
        await process_one(client, pending_id)
    return len(due_ids)


async def run_queue_worker(client: TelegramClient, stop_event: asyncio.Event) -> None:
    """Фоновый цикл воркера."""
    if not settings.queue_enabled:
        log.info("Очередь повторного разбора отключена (QUEUE_ENABLED=false)")
        return

    interval = max(5, settings.queue_poll_seconds)
    log.info("Воркер очереди разбора запущен: опрос раз в %s с", interval)
    while not stop_event.is_set():
        try:
            processed = await process_due_once(client)
            if processed:
                depth = await pending_queue.queue_depth()
                log.info("Очередь разбора: обработано %s, состояние %s", processed, depth)
        except Exception:
            log.exception("Ошибка воркера очереди разбора")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            continue
