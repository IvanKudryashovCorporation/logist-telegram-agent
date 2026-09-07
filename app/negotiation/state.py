"""Состояние переписки с водителем в памяти процесса (какое поле ждём следующим).

Одного работающего инстанса агента достаточно для MVP — как и в app/publishing/service.py.
"""

# tg_user_id водителя -> имя поля Driver, которое агент запросил последним.
_awaiting_field: dict[int, str] = {}

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
