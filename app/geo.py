"""Геокодирование мест из заявок и расстояния — основа фильтра радиуса.

Три слоя:

* :func:`normalize_place` чистит грязное название из заявки («ст. Голубицкая»,
  «Брянка ЛНР», «с. Соленое озеро (Джанкой)») до имени + подсказки региона;
* :func:`resolve` ищет кандидатов в кэше ``geo_places`` и только при промахе
  идёт в Nominatim (OpenStreetMap), соблюдая его лимит ~1 запрос/сек;
* :func:`pick` выбирает одного кандидата для неоднозначного имени.

Веб-слой в запросе ленты в сеть НЕ ходит: :func:`centers_for` читает только
кэш и справочник ``CITY_COORDS``. Сеть — только у фонового воркера
(:mod:`app.services.geocode`).
"""

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from datetime import timedelta
from math import asin, cos, radians, sin, sqrt
from typing import Optional

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.city_aliases import CITY_ALIASES, CITY_COORDS, city_key, split_city_terms
from app.config import settings
from app.models.geo_place import (
    SOURCE_NOMINATIM,
    STATUS_MANUAL,
    STATUS_NOT_FOUND,
    STATUS_OK,
    GeoPlace,
)
from app.timeutil import now_utc_naive

log = logging.getLogger("app.geo")

Coords = tuple[float, float]

#: Допустимые значения радиуса, км (0 — только сам город).
RADIUS_CHOICES: tuple[int, ...] = (0, 25, 50, 100, 200)

#: Километров в градусе широты (и долготы на экваторе).
KM_PER_DEGREE = 111.2

#: Через сколько дней повторяем запрос по имени, которое не нашли.
NOT_FOUND_RETRY = timedelta(days=7)

_NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
_COUNTRY_CODES = "ru,ua,by,kz,ge"
#: Типы объектов Nominatim, которые считаем «населённым пунктом».
_PLACE_KINDS = frozenset(
    {"city", "town", "village", "hamlet", "suburb", "locality", "isolated_dwelling"}
)
_MAX_CANDIDATES = 5


# --- Расстояния --------------------------------------------------------------


def haversine_km(a: Coords, b: Coords) -> float:
    """Расстояние между двумя точками (lat, lon) в километрах."""
    lat1, lon1, lat2, lon2 = map(radians, (a[0], a[1], b[0], b[1]))
    d_lat = lat2 - lat1
    d_lon = lon2 - lon1
    h = sin(d_lat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(d_lon / 2) ** 2
    return 2 * 6371 * asin(sqrt(h))


def flat_distance_km(center: Coords, point: Coords) -> float:
    """Расстояние «по плоскости» от центра — ровно та формула, что в SQL.

    SQL-фильтр радиуса (:func:`app.web.filters._radius_clause`) считает так же,
    потому что в портативном SQL нет тригонометрии. На 200 км погрешность
    относительно гаверсинуса меньше процента.
    """
    d_lat = (point[0] - center[0]) * KM_PER_DEGREE
    d_lon = (point[1] - center[1]) * KM_PER_DEGREE * cos(radians(center[0]))
    return sqrt(d_lat * d_lat + d_lon * d_lon)


def parse_radius(value) -> int:
    """Радиус из query-параметра: только значения из белого списка, иначе 0."""
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return 0
    return parsed if parsed in RADIUS_CHOICES else 0


# --- Нормализация названий ---------------------------------------------------

_PLACE_PREFIX_RE = re.compile(
    r"^(?:г\.?|гор\.|город|с\.?|село|ст\.?|ст-ца|станица|пгт\.?|пос\.?|посёлок|поселок|"
    r"п\.?|д\.?|деревня|х\.?|хутор|кпп|мкр\.?|аул|рп\.?)\s+",
    re.IGNORECASE,
)

#: Хвосты после запятой, которые описывают не регион, а место внутри города.
_NOT_A_REGION_RE = re.compile(
    r"аэропорт|вокзал|ж/?д|станция|порт|центр|автовокзал|ул\.|улица|пр\.|проспект|"
    r"кпп|пункт|терминал|отель|гостиниц",
    re.IGNORECASE,
)

#: Регион, записанный аббревиатурой (в конце названия или в скобках).
_REGION_ABBREVIATIONS = {
    "лнр": "Луганская область",
    "днр": "Донецкая область",
    "знр": "Запорожская область",
    "хнр": "Херсонская область",
}
_ABBREVIATION_RE = re.compile(
    r"\s*[,(]?\s*\b(" + "|".join(_REGION_ABBREVIATIONS) + r")\b\)?\s*$", re.IGNORECASE
)
_PAREN_RE = re.compile(r"\(([^)]*)\)")


def normalize_place(raw: Optional[str]) -> tuple[str, Optional[str]]:
    """Грязное название из заявки -> ``(чистое имя, подсказка региона | None)``.

    «ст. Голубицкая» -> («Голубицкая», None); «Брянка ЛНР» -> («Брянка», «Луганская
    область»); «Корноухово, Татарстан» -> («Корноухово», «Татарстан»);
    «с. Соленое озеро (Джанкой)» -> («Соленое озеро», «Джанкой»);
    «Мин Воды» -> («Минеральные Воды», None).
    """
    text = re.sub(r"\s+", " ", (raw or "").strip())
    if not text:
        return "", None

    hint: Optional[str] = None

    paren = _PAREN_RE.search(text)
    if paren:
        inside = paren.group(1).strip()
        text = _PAREN_RE.sub(" ", text).strip()
        if inside and not _NOT_A_REGION_RE.search(inside):
            hint = _REGION_ABBREVIATIONS.get(inside.lower(), inside)

    abbreviation = _ABBREVIATION_RE.search(text)
    if abbreviation:
        hint = _REGION_ABBREVIATIONS[abbreviation.group(1).lower()]
        text = text[: abbreviation.start()].strip()

    if "," in text:
        head, _, tail = text.partition(",")
        tail = tail.strip()
        text = head.strip()
        if tail and not _NOT_A_REGION_RE.search(tail):
            hint = hint or tail

    text = _PLACE_PREFIX_RE.sub("", text).strip(" .,;-")

    canonical = _canonical_alias(text)
    return canonical or text, hint


def _canonical_alias(name: str) -> Optional[str]:
    """Точное совпадение со справочником сокращений (без префиксного — он
    превратил бы деревню «Краснодарский» в Краснодар)."""
    key = city_key(name)
    if key in CITY_ALIASES:
        return CITY_ALIASES[key]
    squeezed = key.replace(" ", "")
    if squeezed in CITY_ALIASES:
        return CITY_ALIASES[squeezed]
    return None


def place_key(name: str, hint: Optional[str]) -> str:
    """Ключ кэша: нормализованное имя | подсказка региона."""
    return f"{city_key(name)}|{city_key(hint) if hint else ''}"[:200]


# --- Кандидаты ---------------------------------------------------------------


@dataclass(frozen=True)
class Candidate:
    lat: float
    lon: float
    name: str = ""
    kind: str = ""

    @property
    def coords(self) -> Coords:
        return (self.lat, self.lon)

    def as_json(self) -> dict:
        return {"lat": self.lat, "lon": self.lon, "name": self.name, "kind": self.kind}

    @classmethod
    def from_json(cls, data: dict) -> "Candidate":
        return cls(
            lat=float(data["lat"]), lon=float(data["lon"]),
            name=str(data.get("name") or ""), kind=str(data.get("kind") or ""),
        )


def pick(candidates: list[Candidate], near: Optional[Coords] = None) -> Optional[Candidate]:
    """Один кандидат для неоднозначного имени.

    Если известен другой конец маршрута — берём ближайшего к нему («Орджоникидзе
    → Феодосия» даст крымское). Иначе первого: Nominatim отдаёт по значимости.
    """
    if not candidates:
        return None
    if near is None or len(candidates) == 1:
        return candidates[0]
    return min(candidates, key=lambda c: haversine_km(c.coords, near))


#: Если адрес — сам населённый пункт и он дальше этого от найденной деревни,
#: верим адресу, а не названию (см. :func:`refine_by_address`).
ADDRESS_OVERRIDE_KM = 100

#: Типы «мелких» мест: только им адрес может возразить. Город из справочника
#: или областной центр («Краснодар», адрес «Центральный») не трогаем.
_SMALL_PLACE_KINDS = frozenset({"village", "hamlet", "locality", "isolated_dwelling", "suburb"})

#: Адрес, который заведомо не название населённого пункта.
_NOT_A_PLACE_ADDRESS_RE = re.compile(
    r"\d|ул\.|улица|пр-т|пр\.|проспект|пер\.|переулок|шоссе|наб\.|набережная|бул\.|бульвар|"
    r"площадь|пл\.|аэропорт|вокзал|кпп|порт|центр|отель|гостиниц|санатор|пансионат|"
    r"тц|трц|трк|рынок|больниц|школ|гер\.|героев|дом|д\.",
    re.IGNORECASE,
)


def _same_name(candidate: "Candidate", name: str) -> bool:
    """Первый компонент названия кандидата («Оползневое, Симеизский…») == имя."""
    head = candidate.name.split(",")[0]
    return city_key(head) == city_key(name)


async def refine_by_address(
    session: AsyncSession,
    chosen: Optional["Candidate"],
    address: Optional[str],
    near: Optional[Coords] = None,
) -> Optional["Candidate"]:
    """Исправляет заведомо неверно найденную деревню по адресу из заявки.

    Случай с прода: «Мрия», адрес «Оползневое» — Nominatim знает только село
    Мрия под Киевом, и заказ из Крыма получил 1100 км до Севастополя. Если адрес
    — это название населённого пункта, которое геокодер нашёл ровно в одном
    месте, а выбранная деревня находится от него дальше
    :data:`ADDRESS_OVERRIDE_KM`, берём координаты адреса.

    Осторожно, чтобы не ломать правильное: улицы, дома, аэропорты и т.п. сюда
    не попадают, крупные города и места из справочника не переопределяются.
    Бросает :class:`GeocoderUnavailable`, как и :func:`resolve`.
    """
    if chosen is None or chosen.kind not in _SMALL_PLACE_KINDS:
        return chosen
    raw = (address or "").strip()
    if not raw or len(raw) > 60 or _NOT_A_PLACE_ADDRESS_RE.search(raw):
        return chosen
    name, _ = normalize_place(raw)
    if not name or city_key(name) == city_key(normalize_place(chosen.name.split(",")[0])[0]):
        return chosen

    matches = [c for c in await resolve(session, raw) if _same_name(c, name)]
    if len(matches) != 1:
        return chosen
    by_address = matches[0]
    if haversine_km(chosen.coords, by_address.coords) <= ADDRESS_OVERRIDE_KM:
        return chosen
    log.info(
        "Адрес «%s» противоречит найденному месту «%s» — беру координаты адреса",
        raw, chosen.name[:60],
    )
    return by_address


def _seed_candidates(name: str) -> Optional[list[Candidate]]:
    coords = CITY_COORDS.get(name)
    if coords is None:
        return None
    return [Candidate(lat=coords[0], lon=coords[1], name=name, kind="city")]


# --- Кэш и Nominatim ---------------------------------------------------------


class GeocoderUnavailable(Exception):
    """Сеть/Nominatim недоступны — результат НЕ кэшируем, попробуем позже."""


_nominatim_lock = asyncio.Lock()
_last_request_at = 0.0


async def _nominatim_search(query: str) -> list[Candidate]:
    """Один запрос к Nominatim с соблюдением лимита 1 запрос в секунду."""
    global _last_request_at
    async with _nominatim_lock:
        wait = 1.1 - (time.monotonic() - _last_request_at)
        if wait > 0:
            await asyncio.sleep(wait)
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                response = await client.get(
                    _NOMINATIM_URL,
                    params={
                        "q": query,
                        "format": "jsonv2",
                        "limit": _MAX_CANDIDATES,
                        "countrycodes": _COUNTRY_CODES,
                        "accept-language": "ru",
                    },
                    headers={"User-Agent": settings.geocoder_user_agent},
                )
            response.raise_for_status()
            rows = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise GeocoderUnavailable(str(exc)) from exc
        finally:
            _last_request_at = time.monotonic()

    candidates = []
    for row in rows:
        kind = row.get("addresstype") or row.get("type") or ""
        if kind not in _PLACE_KINDS and row.get("type") not in _PLACE_KINDS:
            continue
        try:
            candidates.append(
                Candidate(
                    lat=float(row["lat"]), lon=float(row["lon"]),
                    name=str(row.get("display_name") or "")[:160], kind=kind,
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    return candidates


async def _cached(session: AsyncSession, key: str) -> Optional[GeoPlace]:
    return (
        await session.execute(select(GeoPlace).where(GeoPlace.key == key))
    ).scalar_one_or_none()


def _is_fresh(place: GeoPlace) -> bool:
    """Годна ли запись кэша без повторного запроса."""
    if place.status in (STATUS_OK, STATUS_MANUAL):
        return True
    if place.checked_at is None:
        return False
    return now_utc_naive() - place.checked_at < NOT_FOUND_RETRY


async def resolve(session: AsyncSession, raw: Optional[str]) -> list[Candidate]:
    """Кандидаты для названия места: справочник -> кэш -> Nominatim.

    Бросает :class:`GeocoderUnavailable`, если нужен запрос, а сеть недоступна.
    Для имени без результатов возвращает пустой список (и кэширует это).
    """
    name, hint = normalize_place(raw)
    if not name:
        return []

    seeded = _seed_candidates(name)
    if seeded is not None:
        return seeded

    key = place_key(name, hint)
    place = await _cached(session, key)
    if place is not None and _is_fresh(place):
        return [Candidate.from_json(item) for item in place.candidates]

    if not settings.geocoding_enabled:
        return []

    query = f"{name}, {hint}" if hint else name
    found = await _nominatim_search(query)

    if place is None:
        place = GeoPlace(key=key)
        session.add(place)
    place.candidates = [c.as_json() for c in found]
    place.status = STATUS_OK if found else STATUS_NOT_FOUND
    place.source = SOURCE_NOMINATIM
    place.checked_at = now_utc_naive()
    await session.commit()
    return found


# --- Для веб-слоя: только кэш, без сети --------------------------------------


async def cached_center(session: AsyncSession, raw: str) -> Optional[Coords]:
    """Центр для радиуса по названию из фильтра — без обращения к сети."""
    name, hint = normalize_place(raw)
    if not name:
        return None

    seeded = _seed_candidates(name)
    if seeded is not None:
        return seeded[0].coords

    place = await _cached(session, place_key(name, hint))
    if place is None and hint is None:
        # Название выбрано в подсказках «как есть» — регион мог быть в заявке.
        prefix = f"{city_key(name)}|"
        place = (
            await session.execute(
                select(GeoPlace)
                .where(GeoPlace.key.startswith(prefix, autoescape=True))
                .where(GeoPlace.status.in_((STATUS_OK, STATUS_MANUAL)))
                .order_by(GeoPlace.id)
                .limit(1)
            )
        ).scalar_one_or_none()
    if place is None or place.status not in (STATUS_OK, STATUS_MANUAL) or not place.candidates:
        return None
    return Candidate.from_json(place.candidates[0]).coords


async def centers_for(
    session: AsyncSession, raw_filter: str
) -> tuple[dict[str, Coords], list[str]]:
    """Центры радиуса по терминам из фильтра («Краснодар, Сочи»).

    Возвращает ``({термин: (lat, lon)}, [термины без координат])``.
    """
    centers: dict[str, Coords] = {}
    missing: list[str] = []
    for term in split_city_terms(raw_filter):
        coords = await cached_center(session, term)
        if coords is None:
            missing.append(term)
        else:
            centers[term] = coords
    return centers, missing
