"""Структурные фильтры ленты: откуда/куда, дата и время подачи, цена, пассажиры.

Все поля опциональны и комбинируются по И.

Здесь же — ДВЕ реализации одной семантики:

* :meth:`Filters.sql` — условия WHERE для SQLAlchemy. Именно они работают на
  проде: раньше фильтр применялся перебором уже выбранных строк в Python,
  из-за чего пагинация была невозможна в принципе (страницу нельзя отрезать,
  пока не отфильтруешь всё).
* :meth:`Filters.matches` — эталонная проверка одной строки. В проде не
  используется, но её покрывает тест, который сверяет результаты SQL и Python
  на одном наборе данных: если кто-то поправит одну реализацию и забудет
  другую, тест упадёт.

Радиус «откуда/куда»: к совпадению по названию добавляется «ИЛИ координаты в
круге вокруг города». Круг считается по заранее найденным центрам
(``from_centers``/``to_centers`` — их кладёт роут из кэша геокодера), так что
``sql()`` остаётся синхронным и сам в БД за центрами не ходит. Заказы без
координат по-прежнему находятся по названию — радиус ничего не отнимает.

Исправленный баг: фильтр «Пассажиров (мин.)» раньше отбрасывал заявки, где
число пассажиров не указано (``None`` трактовался как 0). Диспетчер часто не
пишет пассажиров — получалось, что фильтр «от 1» прятал большую часть ленты.
Теперь неизвестное число пассажиров НЕ отсеивается.
"""

from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation
from math import cos, radians
from typing import Optional

from sqlalchemy import and_, or_

from app.city_aliases import canonical_city_variants, city_matches, split_city_terms
from app.geo import KM_PER_DEGREE, Coords, flat_distance_km, parse_radius
from app.models import Order


class Filters:
    """Структурные фильтры ленты."""

    def __init__(
        self,
        from_city: str = "",
        to_city: str = "",
        passengers: str = "",
        date_from: str = "",
        date_to: str = "",
        time_from: str = "",
        time_to: str = "",
        price_min: str = "",
        price_max: str = "",
        from_radius: str = "",
        to_radius: str = "",
    ) -> None:
        self.from_city = (from_city or "").strip()
        self.to_city = (to_city or "").strip()
        self.passengers = self._parse_int(passengers)
        self.date_from = self._parse_date(date_from)
        self.date_to = self._parse_date(date_to)
        self.time_from = self._parse_time(time_from)
        self.time_to = self._parse_time(time_to)
        self.price_min = self._parse_decimal(price_min)
        self.price_max = self._parse_decimal(price_max)
        self.from_radius = parse_radius(from_radius)
        self.to_radius = parse_radius(to_radius)
        # Центры кругов: {термин из фильтра: (lat, lon)}; заполняет роут.
        self.from_centers: dict[str, Coords] = {}
        self.to_centers: dict[str, Coords] = {}
        #: Термины, для которых радиус запрошен, но координат нет.
        self.radius_missing: list[str] = []

    # --- разбор сырых query-параметров --------------------------------------

    @staticmethod
    def _parse_int(value) -> Optional[int]:
        try:
            parsed = int(str(value).strip())
        except (TypeError, ValueError):
            return None
        # Отрицательное/нулевое число пассажиров смысла не имеет.
        return parsed if parsed > 0 else None

    @staticmethod
    def _parse_date(value) -> Optional[date]:
        raw = str(value).strip()
        try:
            return datetime.strptime(raw, "%Y-%m-%d").date()
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _parse_time(value) -> Optional[time]:
        raw = str(value).strip()
        for fmt in ("%H:%M", "%H:%M:%S"):
            try:
                return datetime.strptime(raw, fmt).time()
            except ValueError:
                continue
        return None

    @staticmethod
    def _parse_decimal(value) -> Optional[Decimal]:
        try:
            parsed = Decimal(str(value).strip())
        except (TypeError, ValueError, InvalidOperation):
            return None
        return parsed if parsed >= 0 else None

    # --- служебное -----------------------------------------------------------

    @property
    def active_count(self) -> int:
        # Радиус сам по себе не фильтр — он лишь расширяет «откуда/куда».
        return sum(
            1
            for value in (
                self.from_city, self.to_city, self.passengers, self.date_from,
                self.date_to, self.time_from, self.time_to, self.price_min,
                self.price_max,
            )
            if value is not None and value != ""
        )

    def as_dict(self) -> dict:
        """Значения строками, как их ожидает форма — для ссылок пагинации."""
        return {
            "from_city": self.from_city,
            "to_city": self.to_city,
            "passengers": str(self.passengers) if self.passengers is not None else "",
            "date_from": self.date_from.isoformat() if self.date_from else "",
            "date_to": self.date_to.isoformat() if self.date_to else "",
            "time_from": self.time_from.strftime("%H:%M") if self.time_from else "",
            "time_to": self.time_to.strftime("%H:%M") if self.time_to else "",
            "price_min": str(self.price_min) if self.price_min is not None else "",
            "price_max": str(self.price_max) if self.price_max is not None else "",
            "from_radius": str(self.from_radius) if self.from_radius else "",
            "to_radius": str(self.to_radius) if self.to_radius else "",
        }

    # --- подпись «~18 км от Краснодара» на карточке --------------------------

    def radius_notes(self, order: Order) -> dict:
        """Подписи для карточки: чем заказ попал в выборку, если не названием.

        Без них водитель не понимает, почему в ленте посёлок вместо города.
        """
        return {
            "from": _radius_note(
                self.from_city, order.from_city, order.from_lat, order.from_lon,
                self.from_radius, self.from_centers,
            ),
            "to": _radius_note(
                self.to_city, order.to_city, order.to_lat, order.to_lon,
                self.to_radius, self.to_centers,
            ),
        }

    # --- SQL -----------------------------------------------------------------

    def sql(self) -> list:
        """Условия WHERE, эквивалентные :meth:`matches`."""
        clauses: list = []

        from_clause = _city_clause(
            Order.from_city_key, self.from_city,
            Order.from_lat, Order.from_lon, self.from_radius, self.from_centers,
        )
        if from_clause is not None:
            clauses.append(from_clause)
        to_clause = _city_clause(
            Order.to_city_key, self.to_city,
            Order.to_lat, Order.to_lon, self.to_radius, self.to_centers,
        )
        if to_clause is not None:
            clauses.append(to_clause)

        if self.passengers is not None:
            # Неизвестное число пассажиров (NULL) НЕ отсеиваем — см. docstring.
            clauses.append(
                or_(
                    Order.passengers.is_(None),
                    Order.passengers >= self.passengers,
                )
            )

        if self.date_from is not None:
            clauses.append(Order.pickup_at >= datetime.combine(self.date_from, time.min))
        if self.date_to is not None:
            clauses.append(Order.pickup_at <= datetime.combine(self.date_to, time.max))

        # Время суток сравниваем по подготовленной строке 'HH:MM': извлекать
        # TIME из datetime переносимо не получается (см. Order.pickup_time_key).
        # NULL pickup_time_key сравнение в SQL не проходит — строка отсеивается,
        # что совпадает с matches().
        if self.time_from is not None:
            clauses.append(Order.pickup_time_key >= self.time_from.strftime("%H:%M"))
        if self.time_to is not None:
            clauses.append(Order.pickup_time_key <= self.time_to.strftime("%H:%M"))

        if self.price_min is not None:
            clauses.append(Order.client_price >= self.price_min)
        if self.price_max is not None:
            clauses.append(Order.client_price <= self.price_max)

        return clauses

    # --- эталонная проверка одной строки (для тестов) ------------------------

    def matches(self, order: Order) -> bool:
        if self.from_city and not _side_matches(
            self.from_city, order.from_city, order.from_lat, order.from_lon,
            self.from_radius, self.from_centers,
        ):
            return False
        if self.to_city and not _side_matches(
            self.to_city, order.to_city, order.to_lat, order.to_lon,
            self.to_radius, self.to_centers,
        ):
            return False
        # Неизвестное число пассажиров (None) намеренно НЕ отсеивается:
        # диспетчеры часто его не пишут, и фильтр «от 1» прятал бы пол-ленты.
        if (
            self.passengers is not None
            and order.passengers is not None
            and order.passengers < self.passengers
        ):
            return False
        if self.date_from is not None and (
            order.pickup_at is None or order.pickup_at.date() < self.date_from
        ):
            return False
        if self.date_to is not None and (
            order.pickup_at is None or order.pickup_at.date() > self.date_to
        ):
            return False
        if self.time_from is not None and (
            order.pickup_at is None or order.pickup_at.time() < self.time_from
        ):
            return False
        if self.time_to is not None and (
            order.pickup_at is None or order.pickup_at.time() > self.time_to
        ):
            return False
        if self.price_min is not None and (
            order.client_price is None or order.client_price < self.price_min
        ):
            return False
        if self.price_max is not None and (
            order.client_price is None or order.client_price > self.price_max
        ):
            return False
        return True


def _city_clause(column, raw: str, lat_column, lon_column, radius: int, centers: dict):
    """«Город из фильтра совпадает с городом заказа» — с учётом сокращений.

    Несколько городов через запятую = ИЛИ. Каждый термин раскрывается во все
    известные написания (``canonical_city_variants``), плюс подстрочное
    совпадение для достаточно длинных вариантов — LLM пишет и «Симферополь,
    аэропорт», что точным равенством не поймать.

    При ``radius > 0`` и известном центре термина к этому добавляется «или
    координаты заказа лежат в круге радиуса ``radius`` км вокруг центра».
    """
    terms = split_city_terms(raw)
    if not terms:
        return None

    term_clauses: list = []
    for term in terms:
        variants = canonical_city_variants(term)
        if not variants:
            continue
        sub = [column.in_(variants)]
        sub += [column.contains(v, autoescape=True) for v in variants if len(v) >= 4]
        clause = or_(*sub)

        center = centers.get(term) if radius else None
        if center is not None:
            clause = or_(clause, _radius_clause(lat_column, lon_column, center, radius))
        term_clauses.append(clause)

    if not term_clauses:
        return None
    return or_(*term_clauses)


def _radius_clause(lat_column, lon_column, center: Coords, radius: int):
    """Точка в круге радиуса ``radius`` км — арифметикой, без тригонометрии в SQL.

    Та же формула, что в :func:`app.geo.flat_distance_km`. Прямоугольник по
    широте/долготе отсекает основную массу строк до умножений; ``cos`` центра
    считается здесь, в Python, и уходит в запрос константой.
    """
    lat0, lon0 = center
    cos0 = max(cos(radians(lat0)), 0.01)
    d_lat = radius / KM_PER_DEGREE
    d_lon = radius / (KM_PER_DEGREE * cos0)
    dy = (lat_column - lat0) * KM_PER_DEGREE
    dx = (lon_column - lon0) * (KM_PER_DEGREE * cos0)
    return and_(
        lat_column.between(lat0 - d_lat, lat0 + d_lat),
        lon_column.between(lon0 - d_lon, lon0 + d_lon),
        dy * dy + dx * dx <= radius * radius,
    )


def _side_matches(
    raw_filter: str,
    order_city: Optional[str],
    lat: Optional[float],
    lon: Optional[float],
    radius: int,
    centers: dict,
) -> bool:
    """Python-версия :func:`_city_clause` для эталонной проверки."""
    terms = split_city_terms(raw_filter)
    if not terms:
        return True
    for term in terms:
        if city_matches(term, order_city):
            return True
        center = centers.get(term) if radius else None
        if (
            center is not None
            and lat is not None
            and lon is not None
            and flat_distance_km(center, (lat, lon)) <= radius
        ):
            return True
    return False


def _radius_note(
    raw_filter: str,
    order_city: Optional[str],
    lat: Optional[float],
    lon: Optional[float],
    radius: int,
    centers: dict,
) -> Optional[str]:
    if not radius or not centers or lat is None or lon is None:
        return None
    terms = split_city_terms(raw_filter)
    if any(city_matches(term, order_city) for term in terms):
        return None  # совпало по названию — пояснять нечего
    nearest = min(
        ((flat_distance_km(center, (lat, lon)), term) for term, center in centers.items()),
        default=None,
    )
    if nearest is None or nearest[0] > radius:
        return None
    # Название в кавычках: склонять произвольные названия («от Краснодара») нельзя.
    return f"~{max(1, round(nearest[0]))} км от «{nearest[1]}»"
