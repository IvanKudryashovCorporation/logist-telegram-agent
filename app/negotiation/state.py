"""Состояние переписки с водителем в памяти процесса (какое поле ждём следующим).

Одного работающего инстанса агента достаточно для MVP — как и в app/publishing/service.py.
"""

from decimal import Decimal

# tg_user_id водителя -> имя поля Driver, которое агент запросил последним.
_awaiting_field: dict[int, str] = {}

# (chat_id, message_id) уже обработанных сообщений — защита от повторной доставки
# одного и того же события Telethon (например после реконнекта/get_difference).
_processed_messages: set[tuple[int, int]] = set()
_PROCESSED_MESSAGES_LIMIT = 5000

# tg_user_id водителя -> сколько раз подряд агент не смог разобрать его сообщение.
_unclear_streak: dict[int, int] = {}
UNCLEAR_STREAK_LIMIT = 2

# tg_user_id водителя -> сумма, которую агент последней предложил при торге
# (чтобы не откатиться на базовую оплату, если водитель потом согласится).
_last_offer: dict[int, Decimal] = {}


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


def set_last_offer(tg_user_id: int, amount: Decimal) -> None:
    _last_offer[tg_user_id] = amount


def pop_last_offer(tg_user_id: int) -> Decimal | None:
    return _last_offer.pop(tg_user_id, None)


def bump_unclear_streak(tg_user_id: int) -> int:
    _unclear_streak[tg_user_id] = _unclear_streak.get(tg_user_id, 0) + 1
    return _unclear_streak[tg_user_id]


def reset_unclear_streak(tg_user_id: int) -> None:
    _unclear_streak.pop(tg_user_id, None)

# Порядок сбора обязательных данных (вопрос 81).
REQUIRED_FIELDS_ORDER = [
    "name",
    "phone",
    "car_model",
    "car_plate",
    "photo_exterior_file_id",
    "photo_interior_file_id",
]

FIELD_PROMPTS = {
    "name": "Как вас зовут?",
    "phone": "Ваш номер телефона?",
    "car_model": "Марка и модель авто?",
    "car_plate": "Госномер авто?",
    "photo_exterior_file_id": "Пришлите фото авто снаружи.",
    "photo_interior_file_id": "Пришлите фото салона.",
}


def set_awaiting(tg_user_id: int, field: str | None) -> None:
    if field is None:
        _awaiting_field.pop(tg_user_id, None)
    else:
        _awaiting_field[tg_user_id] = field


def get_awaiting(tg_user_id: int) -> str | None:
    return _awaiting_field.get(tg_user_id)


def next_missing_field(driver) -> str | None:
    for field in REQUIRED_FIELDS_ORDER:
        if not getattr(driver, field):
            return field
    return None
