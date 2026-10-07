"""Уведомления владельца проекта в Telegram.

Проблема, которую это решает: агент может молча перестать разбирать заявки
(кончилась квота LLM, отвалилась сессия, Telegram забанил аккаунт), а внешне
всё выглядит рабочим — сайт открывается, просто лента не пополняется. Узнаёт
об этом владелец обычно от водителей, через сутки.

Уведомления уходят тому же Telethon-клиенту, которым агент читает группы,
поэтому отдельный бот-токен не нужен. Адрес задаётся ``NOTIFY_CHAT_ID``:
``@username``, числовой id чата или ``me`` (своё «Избранное»). Пустое
значение — уведомления выключены, всё остаётся в логе.

Чтобы не превратить чат в спам, есть два предохранителя: минимальный интервал
между отправками и счётчик ПОДРЯД идущих ошибок (одна случайная ошибка сети
алерт не поднимает).
"""

import asyncio
import logging
import time
from typing import Optional, Union

log = logging.getLogger("app.notify")

#: Минимальный интервал между двумя алертами, секунд.
_DEFAULT_MIN_INTERVAL = 300.0


class Notifier:
    """Отправка алертов владельцу с подавлением флуда."""

    def __init__(self) -> None:
        self._client = None
        self._lock = asyncio.Lock()
        self._last_sent_monotonic: float = 0.0
        self._consecutive_errors: int = 0
        self._suppressed: int = 0
        self._sent_total: int = 0

    # --- подключение клиента -------------------------------------------------

    def attach(self, client) -> None:
        """Привязывает Telethon-клиент (вызывается из main.py после start())."""
        self._client = client

    def detach(self) -> None:
        self._client = None

    @property
    def target(self) -> Optional[Union[str, int]]:
        raw = _settings().notify_chat_id.strip()
        if not raw:
            return None
        # Числовой id чата Telethon хочет именно int, а не строку.
        if raw.lstrip("-").isdigit():
            return int(raw)
        return raw

    @property
    def enabled(self) -> bool:
        return self._client is not None and self.target is not None

    # --- отправка ------------------------------------------------------------

    async def send(self, text: str, *, min_interval: float = _DEFAULT_MIN_INTERVAL) -> bool:
        """Отправляет сообщение. Возвращает True, если реально отправили."""
        if not self.enabled:
            log.info("[notify выключен] %s", text)
            return False

        async with self._lock:
            now = time.monotonic()
            if self._last_sent_monotonic and now - self._last_sent_monotonic < min_interval:
                self._suppressed += 1
                log.debug("Уведомление подавлено (слишком часто): %s", text)
                return False
            try:
                await self._client.send_message(self.target, text)
            except Exception:
                log.exception("Не удалось отправить уведомление в Telegram")
                return False
            self._last_sent_monotonic = now
            self._sent_total += 1
            log.info("Уведомление отправлено: %s", text.splitlines()[0][:120])
            return True

    # --- ошибки разбора ------------------------------------------------------

    async def note_parse_error(self, detail: str) -> None:
        """Считает подряд идущие ошибки разбора; при пороге — алертует."""
        self._consecutive_errors += 1
        threshold = max(1, _settings().notify_error_threshold)
        if self._consecutive_errors < threshold:
            return
        count = self._consecutive_errors
        self._consecutive_errors = 0
        await self.send(
            f"⚠️ Агрегатор: {count} ошибок разбора подряд.\n"
            f"Последняя: {detail[:500]}\n"
            "Заявки из этих сообщений лежат в очереди pending_messages — "
            "проверьте ключ/квоту LLM, дальше скриптом scripts/retry_queue.py."
        )

    async def note_parse_success(self) -> None:
        """Успешный разбор сбрасывает счётчик подряд идущих ошибок."""
        self._consecutive_errors = 0

    def stats(self) -> dict[str, int]:
        return {
            "sent": self._sent_total,
            "suppressed": self._suppressed,
            "consecutive_errors": self._consecutive_errors,
        }


def _settings():
    """Импорт настроек отложен, чтобы модуль можно было импортировать до config."""
    from app.config import settings

    return settings


#: Единственный экземпляр на процесс.
notifier = Notifier()
