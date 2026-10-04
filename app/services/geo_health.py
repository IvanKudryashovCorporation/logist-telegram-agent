"""Что не так с расстояниями: отчёт для админки.

Неверные или пропавшие километры раньше замечал только водитель, открыв заказ.
Здесь те же случаи находятся заранее, по живым заказам ленты:

* ``no_coords`` — геокодер не нашёл место (название нет в OpenStreetMap);
* ``same_point`` — начало и конец маршрута совпали (город указан дважды);
* ``no_route`` — координаты есть, но дороги между ними OSRM не нашёл;
* ``odd_rate`` — расстояние посчитано, но цена за км нереальна (меньше
  ``MIN_RATE`` или больше ``MAX_RATE`` ₽/км): почти всегда это значит, что одно
  из мест найдено не там («Вышка» в Закарпатье вместо Астраханской области);
* ``pending`` — просто ещё не дошла очередь, проблемой не считается.
"""

from dataclasses import dataclass
from typing import Optional

from sqlalchemy import select

from app.models import Order
from app.web import queries

#: Разумный коридор цены за километр, ₽/км. Вне его заказ попадает в отчёт.
MIN_RATE = 10.0
MAX_RATE = 250.0

REASON_LABELS = {
    "no_coords": "не найдены координаты",
    "same_point": "начало и конец в одной точке",
    "no_route": "дорога между точками не найдена",
    "odd_rate": "подозрительная цена за км — вероятно, место найдено не там",
}


@dataclass(frozen=True)
class DistanceIssue:
    order: Order
    reason: str
    detail: str

    @property
    def label(self) -> str:
        return REASON_LABELS[self.reason]


def classify(order: Order) -> Optional[tuple[str, str]]:
    """``(причина, подробности)`` или ``None``, если с расстоянием всё в порядке."""
    if order.geo_checked_at is None:
        return None  # геокодер ещё не дошёл
    missing = [
        city
        for city, lat in ((order.from_city, order.from_lat), (order.to_city, order.to_lat))
        if lat is None and city
    ]
    if missing:
        return "no_coords", ", ".join(f"«{city}»" for city in missing)
    if order.from_lat is None or order.to_lat is None:
        return None
    same = (order.from_lat, order.from_lon) == (order.to_lat, order.to_lon)
    if same:
        # Поездка внутри одного города («ЖД → аэропорт») или туда-обратно: расстояния
        # нет по смыслу, это не ошибка. Подозрительно только «разные города — одна точка».
        if order.from_city_key and order.from_city_key == order.to_city_key:
            return None
        return "same_point", f"{order.from_city} → {order.to_city}"
    if order.distance_km is None:
        if order.route_checked_at is not None:
            return "no_route", f"{order.from_city} → {order.to_city}"
        return None  # ждёт очереди
    if order.distance_km >= 1 and order.client_price:
        rate = float(order.client_price) / order.distance_km
        if rate < MIN_RATE or rate > MAX_RATE:
            return "odd_rate", f"{order.distance_km:.0f} км, {rate:.0f} ₽/км"
    return None


async def distance_issues(session, *, limit: int = 100) -> list[DistanceIssue]:
    """Заказы ленты с проблемами расстояния, свежие первыми."""
    orders = (
        await session.execute(
            select(Order).where(*queries.feed_conditions()).order_by(Order.id.desc())
        )
    ).scalars().all()
    issues = []
    for order in orders:
        found = classify(order)
        if found is not None:
            issues.append(DistanceIssue(order, found[0], found[1]))
            if len(issues) >= limit:
                break
    return issues
