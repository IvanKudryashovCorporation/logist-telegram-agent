"""Структура, в которую LLM раскладывает свободный текст заявки диспетчера.

Одно сообщение может содержать НЕСКОЛЬКО заявок сразу (диспетчер списком
скидывает пачку поездок одним сообщением) — поэтому инструмент всегда
возвращает МАССИВ заявок, даже если она одна. Пустой массив — сообщение не
является заявкой вовсе (обычная переписка).
"""

from decimal import Decimal
from typing import Optional

from pydantic import BaseModel

_RECORD_ORDERS_NAME = "record_orders"
_RECORD_ORDERS_DESCRIPTION = (
    "Записать все заявки на перевозку, найденные в этом сообщении. Если в "
    "сообщении несколько поездок (списком, через пустую строку и т.п.) — "
    "каждая идёт отдельным элементом массива orders, не смешивать их поля "
    "в одну заявку. Если сообщение не является заявкой (обычная переписка, "
    "вопрос, подтверждение, 'ок' и т.п.) — массив orders пустой."
)

_ORDER_ITEM_PROPERTIES = {
    "raw_snippet": {
        "type": "string",
        "description": (
            "Точная часть исходного текста сообщения, относящаяся именно к "
            "этой поездке (скопировать как есть). Если в сообщении всего "
            "одна заявка — это всё сообщение целиком."
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
    "passengers": {
        "type": ["integer", "null"],
        "description": "Число пассажиров, если указано. Иначе null (не оставлять поле пустым).",
    },
    "luggage": {
        "type": "string",
        "description": "Особенности багажа, ТОЛЬКО если он объёмный/нестандартный (лыжи, велосипед, много чемоданов). Иначе пусто.",
    },
    "has_pets": {"type": "boolean", "description": "Едут с животным."},
    "needs_child_seat": {"type": "boolean", "description": "Нужно детское кресло."},
    "client_name": {"type": "string", "description": "Имя клиента, если указано."},
    "client_phone": {"type": "string", "description": "Телефон клиента, если указан."},
    "client_price": {
        "type": ["number", "null"],
        "description": "Стоимость для клиента, руб., если указана. Иначе null (не оставлять поле пустым).",
    },
    "is_urgent": {"type": "boolean", "description": "Заявка помечена как срочная."},
    "missing_fields": {
        "type": "array",
        "items": {"type": "string"},
        "description": "Какие ключевые поля отсутствуют или неоднозначны (например 'нет времени подачи', 'не указана точная стоимость'). Пусто, если заявка полная.",
    },
}

_ORDERS_ARRAY_PARAMETERS = {
    "type": "object",
    "properties": {
        "orders": {
            "type": "array",
            "description": "Массив заявок. Пусто, если сообщение не заявка.",
            "items": {
                "type": "object",
                "properties": _ORDER_ITEM_PROPERTIES,
                "required": ["missing_fields"],
            },
        },
    },
    "required": ["orders"],
}

# Формат OpenAI function-calling (DashScope compatible-mode и любой другой
# OpenAI-совместимый провайдер).
PARSE_ORDERS_TOOL_OPENAI = {
    "type": "function",
    "function": {
        "name": _RECORD_ORDERS_NAME,
        "description": _RECORD_ORDERS_DESCRIPTION,
        "parameters": _ORDERS_ARRAY_PARAMETERS,
    },
}


class ParsedOrder(BaseModel):
    raw_snippet: Optional[str] = None
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
