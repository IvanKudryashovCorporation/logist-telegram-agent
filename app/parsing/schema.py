"""Структура, в которую LLM раскладывает свободный текст заявки диспетчера.

Одно сообщение может содержать НЕСКОЛЬКО заявок сразу (диспетчер списком
скидывает пачку поездок одним сообщением) — поэтому инструмент всегда
возвращает МАССИВ заявок, даже если она одна. Пустой массив — сообщение не
является заявкой вовсе (обычная переписка).
"""

from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, field_validator

#: Цена меньше этого — это тысячи: «25» = 25 000 ₽. Междугородний трансфер
#: дешевле 100 ₽ не бывает, а цен 100–999 ₽ на проде не встречалось вовсе.
_THOUSANDS_BELOW = Decimal(100)
#: Цена за поездку не бывает миллионной. От этой суммы считаем, что тысячи
#: применили дважды: диспетчер написал «4000 тыс» (то есть 4000 ₽), а разбор
#: умножил ещё раз и получил 4 000 000.
_IMPLAUSIBLE_FROM = Decimal(300_000)
#: Всё, что и после деления остаётся выше, — явный мусор: цену не показываем.
_ABSURD_ABOVE = Decimal(1_000_000)


def normalize_price(value: Optional[Decimal]) -> Optional[Decimal]:
    """Приводит цену к реальной сумме в рублях (или ``None``, если ей нельзя верить).

    * меньше 100 — диспетчер писал в тысячах («25», «14т»): умножаем на 1000;
    * от 300 000 и кратно 1000 — «4000 тыс» разобрано как 4 000 000: делим на 1000;
    * после этого больше миллиона — цена неизвестна, лучше пустая, чем нереальная.
    """
    if value is None:
        return None
    if 0 < value < _THOUSANDS_BELOW:
        return value * 1000
    for _ in range(2):
        if value >= _IMPLAUSIBLE_FROM and value % 1000 == 0:
            value = value / 1000
    if value > _ABSURD_ABOVE:
        return None
    return value

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
        "description": (
            "Время подачи HH:MM (24ч). Если указан интервал («22-23:00», «с 10 до 11») — "
            "начало интервала. Пусто, если не указано."
        ),
    },
    "from_city": {"type": "string", "description": "Город/населённый пункт отправления."},
    "from_address": {
        "type": "string",
        "description": "Полный адрес отправления, если указан (улица/аэропорт/вокзал и т.п.). Иначе пусто.",
    },
    "to_city": {"type": "string", "description": "Город/населённый пункт назначения."},
    "to_address": {"type": "string", "description": "Полный адрес назначения, если указан. Иначе пусто."},
    "via_points": {
        "type": "array",
        "items": {"type": "string"},
        "description": (
            "Промежуточные пункты маршрута по порядку: города или места, куда нужно заехать между "
            "«откуда» и «куда». «Пермь - Соликамск - Пермь Аэропорт»: from_city «Пермь», via_points "
            "[«Соликамск»], to_city «Пермь Аэропорт». Только остановки по пути, не адреса внутри города "
            "и не повтор «откуда»/«куда». Пусто, если остановок нет."
        ),
    },
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
        "description": (
            "Полная стоимость для клиента в рублях, если указана. Иначе null (не оставлять "
            "поле пустым). Диспетчеры часто пишут в тысячах: «25», «14т», «7.5к», «25+платка» "
            "означают 25000, 14000, 7500 и 25000 ₽ («+платка» — платная дорога сверху, в "
            "цену не входит). Если сумма уже полная (от 1000), а рядом стоит «т»/«тыс»/«т.р.» "
            "(«4000 т», «12000т», «2000 т.р.»), это лишнее слово: цена 4000, 12000 и 2000, "
            "на 1000 НЕ умножай."
        ),
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
    via_points: list[str] = []
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

    @field_validator("client_price")
    @classmethod
    def _price_in_thousands(cls, value: Optional[Decimal]) -> Optional[Decimal]:
        """Страховка поверх промпта: «25+платка» -> 25000, «4000 тыс» -> 4000."""
        return normalize_price(value)
