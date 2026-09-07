"""Структура, в которую LLM раскладывает свободный текст заявки диспетчера."""

from decimal import Decimal
from typing import Optional

from pydantic import BaseModel

# Модель ответа LLM для structured output (tool use).
PARSE_ORDER_TOOL = {
    "name": "record_order",
    "description": "Записать разобранные поля заявки на перевозку.",
    "input_schema": {
        "type": "object",
        "properties": {
            "is_order": {
                "type": "boolean",
                "description": (
                    "Это новая заявка на перевозку (есть маршрут и/или похоже, что диспетчер "
                    "передаёт заказ), а НЕ обычная переписка в чате — вопрос, подтверждение, "
                    "'ок', реплай на чьё-то сообщение и т.п. Если сомневаетесь — false."
                ),
            },
            "pickup_date": {
                "type": "string",
                "description": "Дата подачи в формате YYYY-MM-DD. Если не указан год — текущий или ближайший будущий.",
            },
            "pickup_time": {
                "type": "string",
                "description": "Время подачи HH:MM (24ч). Пусто, если не указано.",
            },
            "from_city": {"type": "string", "description": "Город/населённый пункт отправления."},
            "from_address": {
                "type": "string",
                "description": "Полный адрес отправления, если указан (улица/аэропорт/вокзал и т.п.). Иначе пусто.",
            },
            "to_city": {"type": "string", "description": "Город/населённый пункт назначения."},
            "to_address": {"type": "string", "description": "Полный адрес назначения, если указан. Иначе пусто."},
            "flight_or_train": {
                "type": "string",
                "description": "Номер рейса или поезда, если указан. Иначе пусто.",
            },
            "car_class": {
                "type": "string",
                "description": "Класс/тип авто, ТОЛЬКО если явно нестандартный (минивэн, бизнес, грузовой и т.п.). Обычный седан/эконом — пусто.",
            },
            "passengers": {"type": "integer", "description": "Число пассажиров, если указано."},
            "luggage": {
                "type": "string",
                "description": "Особенности багажа, ТОЛЬКО если он объёмный/нестандартный (лыжи, велосипед, много чемоданов). Иначе пусто.",
            },
            "has_pets": {"type": "boolean", "description": "Едут с животным."},
            "needs_child_seat": {"type": "boolean", "description": "Нужно детское кресло."},
            "client_name": {"type": "string", "description": "Имя клиента, если указано."},
            "client_phone": {"type": "string", "description": "Телефон клиента, если указан."},
            "client_price": {"type": "number", "description": "Стоимость для клиента, руб."},
            "is_urgent": {"type": "boolean", "description": "Заявка помечена как срочная."},
            "missing_fields": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Какие ключевые поля отсутствуют или неоднозначны (например 'нет времени подачи', 'не указана точная стоимость'). Пусто, если заявка полная.",
            },
        },
        "required": ["is_order", "missing_fields"],
    },
}


class ParsedOrder(BaseModel):
    is_order: bool = True
    pickup_date: Optional[str] = None
    pickup_time: Optional[str] = None
    from_city: Optional[str] = None
    from_address: Optional[str] = None
    to_city: Optional[str] = None
    to_address: Optional[str] = None
    flight_or_train: Optional[str] = None
    car_class: Optional[str] = None
    passengers: Optional[int] = None
    luggage: Optional[str] = None
    has_pets: bool = False
    needs_child_seat: bool = False
    client_name: Optional[str] = None
    client_phone: Optional[str] = None
    client_price: Optional[Decimal] = None
    is_urgent: bool = False
    missing_fields: list[str] = []

    @property
    def is_complete(self) -> bool:
        core = (self.from_city, self.to_city, self.client_price)
        return all(core) and not self.missing_fields
