"""Сборка/обновление модели Order из результата LLM-парсинга.

Цена переносится 1 в 1 из заявки, без наценки — это агрегатор для
водителей, не посредник.
"""

import re
from datetime import datetime, timedelta
from typing import Optional

from app.city_aliases import expand_city_term
from app.models import Order, OrderStatus
from app.parsing.schema import ParsedOrder
from app.search import refresh_derived
from app.timeutil import now_msk_naive

# "Писать @username" / "пишите t.me/username" / "писать: @username" — в
# рабочих группах диспетчер часто указывает, кому именно писать по заявке,
# и это не всегда тот, кто прислал сообщение (пересылка, публикация от
# имени бота группы и т.п.). Такой контакт приоритетнее отправителя.
_CONTACT_RE = re.compile(
    r"(?:писать|пишите|пиши)\s*[:\-]?\s*(?:https?://)?(?:t\.me/|@)([A-Za-z0-9_]{4,32})",
    re.IGNORECASE,
)


def extract_contact_username(raw_text: str) -> Optional[str]:
    match = _CONTACT_RE.search(raw_text)
    return match.group(1) if match else None


def combine_pickup_at(pickup_date: Optional[str], pickup_time: Optional[str]) -> Optional[datetime]:
    if not pickup_date:
        return None
    time_part = pickup_time or "00:00"
    try:
        return datetime.fromisoformat(f"{pickup_date}T{time_part}")
    except ValueError:
        return None


#: Время без даты («00:30-1:00 Москва-Демянск»): если оно ушло в прошлое больше
#: чем на это, диспетчер имел в виду завтра — типичный случай заявка около полуночи.
_PAST_TOLERANCE = timedelta(hours=2)
_TIME_RE = re.compile(r"^\s*(\d{1,2}):(\d{2})")


#: Дальше этого срока вперёд подача выглядит подозрительно: диспетчеры публикуют заявки на
#: ближайшие дни, а модель иногда ставит «следующий год» дате без года («28.09» в день 28.09).
FAR_FUTURE = timedelta(days=45)
#: Насколько в прошлое может уйти исправленная дата (заявка о вчерашнем рейсе всё равно протухнет).
_YEAR_FIX_PAST = timedelta(days=3)


def correct_far_year(moment: Optional[datetime], *, now: Optional[datetime] = None) -> Optional[datetime]:
    """Дата подачи, уехавшая вперёд больше чем на :data:`FAR_FUTURE`: возможно, модель поставила
    следующий год. Если тот же день и месяц в этом году — рядом с «сейчас», берём его.

    Иначе возвращаем как есть: реальная заявка на новогодние праздники законна, и угадывать нечего."""
    if moment is None:
        return None
    reference = now or now_msk_naive()
    if moment <= reference + FAR_FUTURE:
        return moment
    try:
        candidate = moment.replace(year=moment.year - 1)
    except ValueError:  # 29 февраля
        return moment
    if reference - _YEAR_FIX_PAST <= candidate <= reference + FAR_FUTURE:
        return candidate
    return moment


def resolve_pickup_at(
    pickup_date: Optional[str],
    pickup_time: Optional[str],
    existing: Optional[datetime] = None,
    *,
    now: Optional[datetime] = None,
) -> Optional[datetime]:
    """Время подачи заказа. Если названа дата — как есть. Если названо только
    время: у уже сохранённого заказа дата остаётся прежней (правка не должна
    сдвигать день), у нового — ближайшее такое время: сегодня или завтра."""
    explicit = combine_pickup_at(pickup_date, pickup_time)
    if explicit is not None:
        return correct_far_year(explicit, now=now)

    match = _TIME_RE.match(pickup_time or "")
    if match is None:
        return existing
    hour, minute = int(match.group(1)), int(match.group(2))
    if hour > 23 or minute > 59:
        return existing

    if existing is not None:
        return existing.replace(hour=hour, minute=minute, second=0, microsecond=0)
    reference = now or now_msk_naive()
    candidate = reference.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate < reference - _PAST_TOLERANCE:
        candidate += timedelta(days=1)
    return candidate


#: Остановок на заявку не больше: дальше это уже не такси-заявка, а перечень адресов.
MAX_VIA_POINTS = 5


def normalized_via_names(parsed: ParsedOrder) -> list[str]:
    """Названия остановок: полные имена городов, без пустых и без подряд идущих повторов."""
    names: list[str] = []
    for raw in parsed.via_points or []:
        name = (expand_city_term(raw.strip()) if raw and raw.strip() else "").strip()
        if name and (not names or names[-1].lower() != name.lower()):
            names.append(name)
    return names[:MAX_VIA_POINTS]


def _apply_via_points(order: Order, parsed: ParsedOrder) -> None:
    """Сохраняет остановки. Те же названия — координаты остаются; изменились — геокодер и маршрут
    пересчитают заказ."""
    names = normalized_via_names(parsed)
    current = order.via_points or []
    if [point.get("name") for point in current] == names:
        return
    order.via_points = [{"name": name, "lat": None, "lon": None} for name in names] or None
    order.geo_checked_at = None
    order.distance_km = None
    order.route_checked_at = None


def has_critical_gaps(order: Order) -> bool:
    """Водителю не хватает главного: города отправления/назначения или цены.

    Остальное, что модель помечает «неоднозначным» («пассажиров 1-2», «28000+ платка», нет
    адреса), заявку не портит: по ней уже можно связаться с диспетчером. Раньше любая такая
    пометка давала плашку «Уточняется», хотя заказ был вполне пригоден."""
    return not (order.from_city and order.to_city) or order.client_price is None


def apply_parsed_fields(order: Order, parsed: ParsedOrder) -> None:
    """Переносит поля из ParsedOrder в Order и выставляет статус по полноте данных."""
    order.contact_username = extract_contact_username(order.raw_text) or order.contact_username
    order.pickup_at = resolve_pickup_at(parsed.pickup_date, parsed.pickup_time, order.pickup_at)
    # Нет времени подачи — заказ «в ближайшее время» (сейчас, «3ч», «в течение
    # часа» или время просто не названо). Названное время снимает метку: правка
    # «в течение часа» -> «в 15:00» делает заказ обычным.
    order.pickup_asap = order.pickup_at is None
    # Раскрываем сокращение сразу при сохранении ("Симф" -> "Симферополь") —
    # иначе один и тот же город хранится по-разному в разных заявках и
    # выглядит как два разных города в автодополнении на сайте.
    order.from_city = (expand_city_term(parsed.from_city) if parsed.from_city else None) or order.from_city
    order.from_address = parsed.from_address or order.from_address
    order.to_city = (expand_city_term(parsed.to_city) if parsed.to_city else None) or order.to_city
    order.to_address = parsed.to_address or order.to_address
    _apply_via_points(order, parsed)
    order.flight_or_train = parsed.flight_or_train or order.flight_or_train
    order.car_class = parsed.car_class or order.car_class
    order.passengers = parsed.passengers or order.passengers
    order.luggage = parsed.luggage or order.luggage
    order.has_pets = parsed.has_pets or order.has_pets
    order.needs_child_seat = parsed.needs_child_seat or order.needs_child_seat
    order.client_name = parsed.client_name or order.client_name
    order.client_phone = parsed.client_phone or order.client_phone
    order.is_urgent = parsed.is_urgent or order.is_urgent

    if parsed.client_price is not None:
        # Цена переносится 1 в 1 из заявки, без наценки: это агрегатор для
        # водителей, а не посредник.
        order.client_price = parsed.client_price

    # Только что созданный заказ ещё без статуса (его проставляет база при записи) — он тоже оценивается.
    if order.status is None or order.status in (OrderStatus.NEW, OrderStatus.NEEDS_CLARIFICATION):
        order.status = OrderStatus.NEEDS_CLARIFICATION if has_critical_gaps(order) else OrderStatus.NEW

    # search_text / from_city_key / to_city_key обязаны соответствовать
    # только что записанным полям, иначе поиск и фильтры на сайте «слепнут».
    refresh_derived(order)
