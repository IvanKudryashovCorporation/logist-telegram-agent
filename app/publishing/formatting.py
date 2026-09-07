"""Текст объявления для водительских групп (вопросы 43-45, 58-60)."""

from app.models import Order


def format_announcement(order: Order) -> str:
    """Без адреса/рейса/бренда/эмодзи. Доп. пометки — только нестандартные (вопросы 58-59)."""
    date_str = order.pickup_at.strftime("%d.%m %H:%M") if order.pickup_at else "время уточняется"
    route = f"{order.from_city or '?'}-{order.to_city or '?'}"

    parts = [date_str, route]
    if order.passengers:
        parts.append(f"{order.passengers}чел")
    payment = order.driver_payment or order.client_price
    if payment is not None:
        parts.append(f"{payment:.0f}")

    lines = [" ".join(parts)]

    extra = []
    if order.car_class:
        extra.append(order.car_class)
    if order.luggage:
        extra.append(order.luggage)
    if order.has_pets:
        extra.append("с животным")
    if order.needs_child_seat:
        extra.append("детское кресло")
    if extra:
        lines.append(", ".join(extra))

    return "\n".join(lines)
