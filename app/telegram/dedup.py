"""Защита от повторной обработки одного и того же события Telethon
(реконнект, get_difference могут переиграть уже обработанное сообщение).

Раньше при переполнении множество очищалось ЦЕЛИКОМ — и сразу после очистки
повторно доставленное старое сообщение обрабатывалось заново, создавая дубли
заявок. Теперь это LRU с постепенным вытеснением самых старых ключей: окно
защиты всегда держит последние N событий, а не обнуляется.

Это best-effort уровень (экономит вызовы LLM). Железную гарантию от дублей
даёт UniqueConstraint ``uq_orders_source`` в БД — память процесса рестарт
агента не переживает.
"""

from collections import OrderedDict
from threading import Lock

#: Сколько последних (chat_id, message_id) держать в памяти.
PROCESSED_MESSAGES_LIMIT = 5000

_processed_messages: "OrderedDict[tuple[int, int, str], None]" = OrderedDict()
_lock = Lock()


def mark_processed(chat_id: int, message_id: int, variant: str = "new") -> bool:
    """True, если это событие обрабатывается впервые (и помечает его обработанным).

    ``variant`` разделяет новое сообщение и каждую его правку: ключ только из
    (chat_id, message_id) отбрасывал ЛЮБУЮ правку уже обработанного сообщения.
    """
    key = (chat_id, message_id, variant)
    with _lock:
        if key in _processed_messages:
            _processed_messages.move_to_end(key)
            return False
        _processed_messages[key] = None
        while len(_processed_messages) > PROCESSED_MESSAGES_LIMIT:
            _processed_messages.popitem(last=False)
        return True


def is_processed(chat_id: int, message_id: int, variant: str = "new") -> bool:
    """Проверка без пометки — нужна воркеру очереди и тестам."""
    with _lock:
        return (chat_id, message_id, variant) in _processed_messages


def unmark_processed(chat_id: int, message_id: int, variant: str = "new") -> None:
    """Снимает пометку — используется, если обработка упала с ошибкой до коммита,
    чтобы повторная доставка того же события не была молча отброшена."""
    with _lock:
        _processed_messages.pop((chat_id, message_id, variant), None)


def clear_processed() -> None:
    """Полный сброс (тесты, принудительный перезапуск разбора)."""
    with _lock:
        _processed_messages.clear()


def processed_count() -> int:
    with _lock:
        return len(_processed_messages)
