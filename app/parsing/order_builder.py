"""Сборка/обновление модели Order из результата LLM-парсинга.

Цена переносится 1 в 1 из заявки, без наценки — это агрегатор для
водителей, не посредник.
"""

import re
from datetime import datetime
from typing import Optional

from app.models import Order, OrderStatus
from app.parsing.schema import ParsedOrder

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


def _combine_pickup_at(pickup_date: Optional[str], pickup_time: Optional[str]) -> Optional[datetime]:
    if not pickup_date:
        return None
    time_part = pickup_time or "00:00"
    try:
        return datetime.fromisoformat(f"{pickup_date}T{time_part}")
    except ValueError:
        return None


def apply_parsed_fields(order: Order, parsed: ParsedOrder) -> None:
    """Переносит поля из ParsedOrder в Order и выставляет статус по полноте данных."""
    order.contact_username = extract_contact_username(order.raw_text) or order.contact_username
    order.pickup_at = _combine_pickup_at(parsed.pickup_date, parsed.pickup_time) or order.pickup_at
    order.from_city = parsed.from_city or order.from_city
    order.from_address = parsed.from_address or order.from_address
    order.to_city = parsed.to_city or order.to_city
    order.to_address = parsed.to_address or order.to_address
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
        order.client_price = parsed.client_price
        order.driver_payment = parsed.client_price  # без наценки, 1 в 1

    if order.status in (OrderStatus.NEW, OrderStatus.NEEDS_CLARIFICATION):
        order.status = (
            OrderStatus.NEEDS_CLARIFICATION if parsed.missing_fields else OrderStatus.NEW
        )
