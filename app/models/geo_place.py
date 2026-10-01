"""Кэш геокодера: название населённого пункта -> кандидаты с координатами.

Nominatim ограничен ~1 запросом в секунду и не должен вызываться дважды за
одно и то же имя, поэтому результат (в том числе «не нашли») хранится здесь.
Ключ — нормализованное имя + подсказка региона (см. app.geo.place_key):
«Орджоникидзе» в Крыму и «Орджоникидзе, Краснодарский край» — разные записи.
"""

from datetime import datetime
from typing import Optional

from sqlalchemy import JSON, DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

STATUS_OK = "ok"
STATUS_NOT_FOUND = "not_found"
#: Координаты заданы вручную (scripts/geo_places.py) — геокодер их не трогает.
STATUS_MANUAL = "manual"

SOURCE_NOMINATIM = "nominatim"
SOURCE_MANUAL = "manual"


class GeoPlace(Base):
    __tablename__ = "geo_places"

    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(200), unique=True, index=True, nullable=False)
    #: Список ``{"lat", "lon", "name", "kind"}`` в порядке значимости (до 5 штук).
    candidates: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=STATUS_OK)
    source: Mapped[str] = mapped_column(String(16), nullable=False, default=SOURCE_NOMINATIM)
    #: Наивный UTC (см. app/timeutil.py). От него считается повтор для not_found.
    checked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=False))

    def __repr__(self) -> str:
        return f"<GeoPlace {self.key!r} {self.status}>"
