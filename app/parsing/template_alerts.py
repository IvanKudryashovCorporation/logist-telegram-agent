"""Уведомления владельцу о сообщениях, которые шаблон бота не разобрал («доработайте шаблон»).

Уходят ботом-сторожем (``ALERT_BOT_TOKEN`` / ``ALERT_CHAT_ID``), как алерты watchdog. Чтобы не завалить чат:
не больше ``TEMPLATE_ALERT_PER_HOUR`` сообщений в час, одно и то же сообщение (повтор из очереди, правка) не
шлётся дважды, а всё, что не влезло в лимит, склеивается в счётчик «ещё N», который уходит с ближайшим уведомлением.
"""

import asyncio
import logging
import time
from collections import Counter, deque
from typing import Awaitable, Callable, Optional

from app.config import settings

log = logging.getLogger("agent.template")

_WINDOW_SECONDS = 3600.0
#: Сколько отпечатков уже отправленных сообщений помним (повторы из очереди не шлём заново).
_SEEN_LIMIT = 500
_TEXT_LIMIT = 1500

_sent_at: "deque[float]" = deque()
_seen: "dict[str, float]" = {}
_suppressed = 0
_suppressed_reasons: "Counter[str]" = Counter()
_tasks: "set[asyncio.Task]" = set()


def reset() -> None:
    """Сброс состояния (тесты)."""
    global _suppressed
    _sent_at.clear()
    _seen.clear()
    _suppressed = 0
    _suppressed_reasons.clear()


async def _default_send(text: str) -> bool:
    from app.services.watchdog import send_telegram

    return await send_telegram(text)


#: Отправка подменяется в тестах.
sender: Callable[[str], Awaitable[bool]] = _default_send


def build_message(chat_id: int, message_id: int, reason: str, text: str, skipped: int, skipped_reasons: "Counter[str]") -> str:
    body = text if len(text) <= _TEXT_LIMIT else text[:_TEXT_LIMIT] + "…"
    lines = [
        "🧩 Шаблон не разобрал заявку — ушла в LLM",
        f"Причина: {reason}",
        f"Чат {chat_id}, сообщение {message_id}",
        "",
        body,
    ]
    if skipped:
        top = ", ".join(f"{why} ×{n}" for why, n in skipped_reasons.most_common(3))
        lines += ["", f"…и ещё {skipped} за последний час не показаны (лимит {settings.template_alert_per_hour}/ч): {top}"]
    return "\n".join(lines)


def notify_failure(chat_id: int, message_id: int, reason: str, text: str, key: str) -> bool:
    """Ставит уведомление в отправку. ``True`` — отправка запланирована, ``False`` — сглажено (дубль или лимит).

    Не блокирует разбор: отправка идёт фоновой задачей, её сбой только пишется в лог."""
    global _suppressed
    now = time.monotonic()
    if key in _seen and now - _seen[key] < 24 * 3600:
        return False
    limit = max(0, settings.template_alert_per_hour)
    while _sent_at and now - _sent_at[0] > _WINDOW_SECONDS:
        _sent_at.popleft()
    if len(_sent_at) >= limit:
        _suppressed += 1
        _suppressed_reasons[reason] += 1
        return False
    _seen[key] = now
    if len(_seen) > _SEEN_LIMIT:
        for stale in sorted(_seen, key=_seen.get)[: _SEEN_LIMIT // 5]:
            _seen.pop(stale, None)
    _sent_at.append(now)
    message = build_message(chat_id, message_id, reason, text, _suppressed, Counter(_suppressed_reasons))
    _suppressed = 0
    _suppressed_reasons.clear()
    try:
        task = asyncio.get_running_loop().create_task(_send(message))
    except RuntimeError:  # нет цикла (вызов вне asyncio) — просто в лог
        log.warning("Шаблон не разобрал заявку: %s", reason)
        return False
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return True


async def _send(message: str) -> None:
    try:
        if not await sender(message):
            log.warning("Уведомление о провале шаблона не доставлено")
    except Exception:
        log.warning("Не удалось отправить уведомление о провале шаблона", exc_info=True)
