"""Защита от повторной обработки одного и того же события Telethon
(реконнект, get_difference могут переиграть уже обработанное сообщение)."""

_processed_messages: set[tuple[int, int]] = set()
_PROCESSED_MESSAGES_LIMIT = 5000


def mark_processed(chat_id: int, message_id: int) -> bool:
    """True, если сообщение обрабатывается впервые (и помечает его обработанным)."""
    key = (chat_id, message_id)
    if key in _processed_messages:
        return False
    if len(_processed_messages) >= _PROCESSED_MESSAGES_LIMIT:
        _processed_messages.clear()
    _processed_messages.add(key)
    return True


def unmark_processed(chat_id: int, message_id: int) -> None:
    """Снимает пометку — используется, если обработка упала с ошибкой до коммита,
    чтобы повторная доставка того же события не была молча отброшена."""
    _processed_messages.discard((chat_id, message_id))
