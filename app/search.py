"""Построение и использование денормализованного поискового поля ``search_text``.

Поиск по ленте раньше делался перебором всех заказов в Python. Чтобы опустить
его в SQL, нужен столбец, который можно искать через ``LIKE '%...%'`` без
функций регистра: ``lower()`` в SQLite понимает только ASCII, поэтому
«симферополь» и «СИМФЕРОПОЛЬ» там не совпадут. Мы приводим текст к нижнему
регистру один раз при записи (Python-овский ``.lower()`` кириллицу знает) и
дальше ищем уже подготовленную строку.
"""

import re
from datetime import datetime
from typing import Any, Iterable, Optional

#: Поля заказа, попадающие в поисковую строку (id добавляется отдельно).
SEARCH_FIELDS: tuple[str, ...] = (
    "client_phone",
    "client_name",
    "from_city",
    "to_city",
    "from_address",
    "to_address",
    "dispatcher_username",
    "contact_username",
)


def build_search_text(order_id: Any, values: Iterable[Optional[Any]]) -> str:
    """Собирает поисковую строку из id и значений полей ``SEARCH_FIELDS``.

    Принимает именно значения (а не объект), чтобы ту же логику можно было
    вызвать из миграции для уже существующих строк.
    """
    parts: list[str] = []
    if order_id is not None:
        parts.append(str(order_id))
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            parts.append(text)
    return " ".join(parts).lower()


def order_search_text(order: Any) -> str:
    """Пересчитывает ``search_text`` для модели заказа."""
    values = [getattr(order, field, None) for field in SEARCH_FIELDS]
    return build_search_text(order.id, values)


def refresh_search_text(order: Any) -> Any:
    """Пересчитывает и присваивает ``order.search_text``; возвращает тот же заказ."""
    order.search_text = order_search_text(order)
    return order


#: Совпадает с порядком ``SEARCH_FIELDS`` — используется миграцией и скриптами.
SEARCH_FIELDS_SQL = ", ".join(SEARCH_FIELDS)


#: Признаки минивэна в классе авто и в тексте заявки. «Альфард» и «спринтер» —
#: марки, которые диспетчеры пишут вместо слова «минивэн».
_MINIVAN_RE = re.compile(
    r"мини-?в[эе]н|компакт-?в[эе]н|\bв[эе]н\b|микроавтобус|минибас|альфард|спринтер|\bvan\b",
    re.IGNORECASE,
)
#: В легковой помещаются четверо пассажиров; пять и больше — уже минивэн.
_MINIVAN_PASSENGERS = 5


def vehicle_type_of(car_class: Optional[str], raw_text: Optional[str], passengers: Optional[int]) -> str:
    """``minivan`` или ``car`` — тип авто, который нужен заказу.

    Минивэн: так назван класс авто, или слово «минивэн» есть в тексте заявки, или
    пассажиров пять и больше. Всё остальное (включая «комфорт», «бизнес» и
    заявки без пометок) — обычная легковая.
    """
    if _MINIVAN_RE.search(car_class or "") or _MINIVAN_RE.search(raw_text or ""):
        return "minivan"
    if passengers is not None and passengers >= _MINIVAN_PASSENGERS:
        return "minivan"
    return "car"


def refresh_derived(order: Any) -> Any:
    """Пересчитывает все денормализованные поля поиска/фильтрации заказа.

    Вызывается перед каждым сохранением (создание и обновление) — в одном
    месте, чтобы колонки не могли «разъехаться» с исходными значениями.
    """
    from app.city_aliases import canonical_city_name, city_key

    refresh_search_text(order)
    # Ключ строится по каноническому названию: «Мин воды» и «Минеральные Воды» —
    # один ключ, какой бы вариант ни остался в поле города.
    from_city = getattr(order, "from_city", None)
    to_city = getattr(order, "to_city", None)
    new_from_key = city_key(canonical_city_name(from_city) or from_city) or None
    new_to_key = city_key(canonical_city_name(to_city) or to_city) or None
    if new_from_key != order.from_city_key or new_to_key != order.to_city_key:
        # Город поменялся (правка сообщения) — старые координаты ему уже не
        # соответствуют, геокодер должен пересчитать оба конца.
        order.from_lat = order.from_lon = order.to_lat = order.to_lon = None
        order.geo_checked_at = None
        order.distance_km = None
        order.route_checked_at = None
    order.from_city_key = new_from_key
    order.to_city_key = new_to_key
    order.pickup_time_key = pickup_time_key(getattr(order, "pickup_at", None))
    order.vehicle_type = vehicle_type_of(
        getattr(order, "car_class", None),
        getattr(order, "raw_text", None),
        getattr(order, "passengers", None),
    )
    return order


def pickup_time_key(pickup_at: Any) -> Optional[str]:
    """Час подачи как 'HH:MM' (или None) — см. Order.pickup_time_key.

    Принимает и ``datetime``, и строку. Строка нужна не «на всякий случай»:
    бэкфилл в миграции читает колонки сырым SQL, а SQLite в таком режиме
    отдаёт DATETIME текстом (``'2026-09-30 12:08:19.849053'``). Без этой
    ветки ``alembic upgrade head`` падал с AttributeError на любой непустой
    базе — то есть ровно там, где бэкфилл и нужен.
    """
    if pickup_at is None:
        return None
    if isinstance(pickup_at, str):
        raw = pickup_at.strip()
        if not raw:
            return None
        try:
            pickup_at = datetime.fromisoformat(raw)
        except ValueError:
            # Нестандартный формат — вытаскиваем часы и минуты напрямую.
            match = re.search(r"(\d{1,2}):(\d{2})", raw)
            if match is None:
                return None
            return f"{int(match.group(1)):02d}:{match.group(2)}"
    return f"{pickup_at.hour:02d}:{pickup_at.minute:02d}"
