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
import difflib
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from math import asin, cos, radians, sin, sqrt
from typing import Optional

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.city_aliases import CITY_COORDS, canonical_city_name, city_key, split_city_terms
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
    {"city", "town", "village", "hamlet", "suburb", "locality", "isolated_dwelling", "aeroway",
     # СНТ и «дачи»: диспетчеры возят именно туда, а в OpenStreetMap это отдельный тип места.
     "allotments"}
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
    r"^(?:сельское\s+поселение|городское\s+поселение|муниципальный\s+округ|городской\s+округ|"
    r"г\.?|гор\.|город|с\.?|село|ст\.?|ст-ца|станица|пгт\.?|пос\.?|посёлок|поселок|"
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

#: Области и края, названия которых встречаются в заявках (в именительном падеже).
_REGION_NAMES = (
    "Алтайский край", "Амурская область", "Архангельская область", "Астраханская область",
    "Белгородская область", "Брянская область", "Владимирская область", "Волгоградская область",
    "Вологодская область", "Воронежская область", "Ивановская область", "Иркутская область",
    "Калининградская область", "Калужская область", "Камчатский край", "Кемеровская область",
    "Кировская область", "Костромская область", "Краснодарский край", "Красноярский край",
    "Курганская область", "Курская область", "Ленинградская область", "Липецкая область",
    "Магаданская область", "Московская область", "Мурманская область", "Нижегородская область",
    "Новгородская область", "Новосибирская область", "Омская область", "Оренбургская область",
    "Орловская область", "Пензенская область", "Пермский край", "Приморский край",
    "Псковская область", "Ростовская область", "Рязанская область", "Самарская область",
    "Саратовская область", "Сахалинская область", "Свердловская область", "Смоленская область",
    "Ставропольский край", "Тамбовская область", "Тверская область", "Томская область",
    "Тульская область", "Тюменская область", "Ульяновская область", "Хабаровский край",
    "Челябинская область", "Забайкальский край", "Ярославская область",
    "Луганская область", "Донецкая область", "Запорожская область", "Херсонская область",
    "Харьковская область", "Сумская область", "Черниговская область", "Киевская область",
    "Николаевская область", "Одесская область", "Днепропетровская область",
)
_REGION_BY_ADJECTIVE = {name.split()[0].lower(): name for name in _REGION_NAMES}
#: «обл.», «р-н» и т.п. — что Nominatim не понимает.
_ABBREV_FIXES = (
    (re.compile(r"\bобл\.?(?=\W|$)", re.IGNORECASE), "область"),
    (re.compile(r"\bр-н\b\.?", re.IGNORECASE), "район"),
    (re.compile(r"\bкр\.(?=\W|$)", re.IGNORECASE), "край"),
    (re.compile(r"\bресп\.?(?=\W|$)", re.IGNORECASE), "республика"),
)
_REGION_TOKEN_RE = re.compile(r"([а-яё\-]+)\s*(обл\.?|област[ьи]|край|кр\.)", re.IGNORECASE)
_DISTRICT_TOKEN_RE = re.compile(r"([а-яё\-]+(?:ский|цкий|ной|ый|ий))\s*(?:р-н|район)", re.IGNORECASE)
_HINT_NEEDS_CLEANING_RE = re.compile(r"обл\b|обл\.|р-н|\bкр\.|\bресп\b|крым|област|район|край", re.IGNORECASE)


def canonical_region(text: Optional[str]) -> Optional[str]:
    """«брянсская обл.» -> «Брянская область», «Крым» -> «Республика Крым», «Выборгский р-н» ->
    «Выборгский район». ``None``, если в тексте региона не видно."""
    raw = re.sub(r"\s+", " ", (text or "").strip())
    if not raw:
        return None
    lowered = raw.lower()
    for abbreviation, full in _REGION_ABBREVIATIONS.items():
        if re.search(rf"\b{abbreviation}\b", lowered):
            return full
    if "крым" in lowered and "севастопол" not in lowered:
        return "Республика Крым"

    token = _REGION_TOKEN_RE.search(lowered)
    if token:
        adjective = token.group(1)
        close = difflib.get_close_matches(adjective, list(_REGION_BY_ADJECTIVE), n=1, cutoff=0.8)
        if close:
            return _REGION_BY_ADJECTIVE[close[0]]
        suffix = "край" if token.group(2).startswith(("кра", "кр")) else "область"
        return f"{adjective.capitalize()} {suffix}"
    district = _DISTRICT_TOKEN_RE.search(lowered)
    if district:
        return f"{district.group(1).capitalize()} район"
    return None


def _clean_hint(hint: Optional[str]) -> Optional[str]:
    """Приводит подсказку региона к виду, который понимает Nominatim.

    Подсказки без «обл./р-н/Крым» («Татарстан», «Джанкой») остаются как есть.
    """
    if not hint or not _HINT_NEEDS_CLEANING_RE.search(hint):
        return hint
    canonical = canonical_region(hint)
    if canonical:
        return canonical
    cleaned = hint
    for pattern, replacement in _ABBREV_FIXES:
        cleaned = pattern.sub(replacement, cleaned)
    return re.sub(r"\s+", " ", cleaned).strip(" ,.") or hint


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
    return canonical or text, _clean_hint(hint)


def _canonical_alias(name: str) -> Optional[str]:
    """Точное совпадение со справочником сокращений (без префиксного — он
    превратил бы деревню «Краснодарский» в Краснодар)."""
    return canonical_city_name(name)


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
    #: Правильное написание, если название в заявке — опечатка («Острогоржск» -> «Острогожск»).
    #: Пусто — название написано верно.
    canonical: str = ""
    #: ``False`` — найдено нечётким поиском без подтверждения регионом: можно доверять, только
    #: если место недалеко от другого конца маршрута (см. :data:`UNVERIFIED_MAX_KM`).
    verified: bool = True

    @property
    def coords(self) -> Coords:
        return (self.lat, self.lon)

    def as_json(self) -> dict:
        data = {"lat": self.lat, "lon": self.lon, "name": self.name, "kind": self.kind}
        if self.canonical:
            data["canonical"] = self.canonical
        if not self.verified:
            data["verified"] = False
        return data

    @classmethod
    def from_json(cls, data: dict) -> "Candidate":
        return cls(
            lat=float(data["lat"]), lon=float(data["lon"]),
            name=str(data.get("name") or ""), kind=str(data.get("kind") or ""),
            canonical=str(data.get("canonical") or ""),
            verified=data.get("verified", True) is not False,
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


#: Когда логика поиска мест менялась в последний раз. Записи кэша «не найдено», проверенные
#: раньше, считаются устаревшими и ищутся заново — иначе улучшение не доходит до мест,
#: которые уже один раз не нашлись (а «не найдено» живёт неделю), и заказы без координат
#: остаются без них. Меняя поиск, обновляйте эту дату.
GEO_LOGIC_DATE = datetime(2026, 10, 5, 8, 32)

#: Версия логики геокодирования. Подняли её — фоновый воркер сам перегеокодирует
#: все заказы, обработанные старой версией: исправления не нужно «накатывать»
#: на прод руками, а старые ошибки (не та деревня) не живут вечно.
GEO_VERSION = 4

#: Часть адреса, называющая регион: «Астраханская область», «Краснодарский край».
_REGION_PART_RE = re.compile(r"област|\bкрай\b|республик", re.IGNORECASE)


def region_hint(address: Optional[str]) -> Optional[str]:
    """Регион из адреса заявки («Ля Дача, Астраханская область, …» -> «Астраханская область»).

    Нет области/края/республики — берём район («Выборгский район»), он тоже сужает поиск.
    """
    district: Optional[str] = None
    for part in (address or "").split(","):
        part = part.strip()
        if not part or len(part) > 60:
            continue
        if _REGION_PART_RE.search(part):
            return canonical_region(part) or part
        if district is None and re.search(r"район|р-н", part, re.IGNORECASE):
            district = canonical_region(part)
    return district


def region_from_text(raw_text: Optional[str], city: Optional[str]) -> Optional[str]:
    """Регион, который диспетчер написал рядом с городом в самой заявке.

    «Село Вершины запорожская обл», «Адлер - Красногвардейский (Крым)»,
    «Кромы ( Орловская обл.)», «Степановка (Курская обл. Рыльский р-н)»: модель
    разбора часто оставляет в названии только город, а регион теряется — и
    Nominatim выбирает однофамильца на другом конце страны.
    """
    base, own_hint = normalize_place(city)
    if not raw_text or not base or own_hint is not None or len(base) < 3:
        return None
    stem = re.escape(base[: max(3, len(base) - 2)])
    for line in raw_text.splitlines():
        match = re.search(stem + r"[а-яё\-]*", line, re.IGNORECASE)
        if not match:
            continue
        tail = line[match.end(): match.end() + 80]
        paren = re.match(r"\s*\(([^)]{2,60})\)", tail)
        if paren:
            candidate = paren.group(1)
        else:
            words = re.match(
                r"\s*,?\s*([а-яё\-]+\s*(?:обл\.?|област\w*|край|р-н|район))", tail, re.IGNORECASE
            )
            candidate = words.group(1) if words else None
        region = canonical_region(candidate) if candidate else None
        if region:
            return region
    return None


def _name_variants(name: str) -> list[str]:
    """«Красногвардейский» -> + «Красногвардейское»: диспетчеры пишут не в том роде."""
    variants = [name]
    if name.endswith(("ский", "цкий", "ный", "ый", "ий")):
        variants.append(re.sub(r"(ский|цкий|ный|ый|ий)$", lambda m: m.group(1)[:-2] + "ое", name))
    return variants


async def _lookup_with_region(
    session: AsyncSession, city: Optional[str], region: Optional[str], near: Optional[Coords]
) -> Optional["Candidate"]:
    """Ищет город уже с регионом: «Вышка» + «Астраханская область».

    Без региона Nominatim отдаёт первую «Вышку» в мире (Закарпатье). Названия, где
    регион указан в самом городе («Брянка ЛНР»), не трогаем.
    """
    if not region or not city:
        return None
    base, own_hint = normalize_place(city)
    if not base or own_hint is not None:
        return None
    for variant in _name_variants(base):
        found = await resolve(session, f"{variant}, {region}")
        if found:
            return pick(found, near)
    return None


def _region_stem(region: str) -> str:
    words = [w for w in re.split(r"\s+", region.lower()) if w not in {"республика", "область", "край", "район"}]
    return (words[0] if words else region.lower())[:5]


#: Если адрес — сам населённый пункт и он дальше этого от найденной деревни,
#: верим адресу, а не названию (см. :func:`refine_by_address`).
ADDRESS_OVERRIDE_KM = 100

#: Типы «мелких» мест: только им адрес может возразить. Город из справочника
#: или областной центр («Краснодар», адрес «Центральный») не трогаем.
_SMALL_PLACE_KINDS = frozenset(
    {"village", "hamlet", "locality", "isolated_dwelling", "suburb", "allotments"}
)

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
    city: Optional[str] = None,
    raw_text: Optional[str] = None,
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
    # Место из справочника (в названии нет запятых) — проверенное, не трогаем.
    seeded = chosen is not None and "," not in chosen.name and chosen.kind == "city"
    if seeded:
        return chosen

    # Регион из адреса или из текста заявки: и когда место не нашли вовсе, и когда
    # нашли однофамильца в другом регионе.
    region = region_hint(address) or region_from_text(raw_text, city)
    if region:
        with_region = await _lookup_with_region(session, city, region, near)
        if with_region is not None:
            if chosen is None or haversine_km(chosen.coords, with_region.coords) > 5:
                log.info("Регион «%s» уточнил место «%s»", region, city)
                return with_region
            return chosen
        # Регион назван, но с ним ничего не нашлось. Найденное без региона годится,
        # только если оно и правда в этом регионе, иначе лучше без координат, чем
        # однофамилец за тысячу километров. Но если регион диспетчер написал прямо в
        # названии («Донецк (ДНР)», «Писково (Московская обл.)»), место уже найдено именно
        # с ним, и район из адреса его не опровергает: «Петровский р-н» — район Донецка,
        # а «Истринский» не совпадает с «Истра» по буквам.
        own_region = normalize_place(city)[1] if city else None
        if (
            chosen is not None
            and own_region is None
            and chosen.kind not in ("near", "region")  # привязка по району/области — уже по названию из заявки
            and _region_stem(region) not in chosen.name.lower()
        ):
            log.info("«%s» (%s): найденное место не в регионе «%s» — координаты не ставим",
                     city, chosen.name[:50], region)
            return None

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
    # Адрес — улика, а не приговор: «тихая гавань» (пляж в Алахадзах) совпала с
    # посёлком Тихая Гавань на Кольском и утащила заказ за 3500 км. Верим адресу, только
    # когда он ближе к другому концу маршрута, чем найденное место (Мрия: Киевская
    # область против Оползневого в Крыму, а едут в Севастополь). Другого конца нет —
    # проверить нечем, оставляем найденное.
    if near is None or haversine_km(by_address.coords, near) >= haversine_km(chosen.coords, near):
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


async def _nominatim_rows(query: str) -> list[dict]:
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
    return rows


async def _nominatim_search(query: str) -> list[Candidate]:
    """Населённые пункты по запросу (города, села, СНТ…)."""
    candidates = []
    for row in await _nominatim_rows(query):
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


def _is_guess(place: GeoPlace) -> bool:
    """Результат — догадка (исправленная опечатка, привязка к району/области/соседу), а не точное
    совпадение названия."""
    return any(
        item.get("canonical") or item.get("kind") in ("near", "region")
        for item in (place.candidates or [])
    )


def _is_fresh(place: GeoPlace) -> bool:
    """Годна ли запись кэша без повторного запроса."""
    if place.status == STATUS_OK and _is_guess(place):
        # Догадки зависят от правил поиска: при смене правил (GEO_LOGIC_DATE) пересчитываем,
        # иначе промахи старого правила («Монастыри» -> «Монастырище») жили бы в кэше вечно.
        return place.checked_at is not None and place.checked_at >= GEO_LOGIC_DATE
    if place.status in (STATUS_OK, STATUS_MANUAL):
        return True
    if place.checked_at is None or place.checked_at < GEO_LOGIC_DATE:
        return False
    return now_utc_naive() - place.checked_at < NOT_FOUND_RETRY


#: Если села нет в OpenStreetMap, но рядом названо известное место, берём его (село обычно
#: в десятках километров). Одноимённое место ближе этого к названному — считаем найденным.
APPROXIMATE_RADIUS_KM = 150


def _is_region_hint(hint: str) -> bool:
    """Подсказка — регион или район, а не соседний населённый пункт."""
    return bool(
        re.search(r"област|край|республик|район|р-н|округ|\bмо\b|\bго\b|\bао\b|муницип", hint, re.IGNORECASE)
    ) or (
        canonical_region(hint) is not None
    )


async def _approximate_by_hint(session: AsyncSession, name: str, hint: str) -> list[Candidate]:
    """«Хлебодаровка (Волноваха)»: села нет в OSM — ставим рядом с Волновахой.

    Сначала ищем одноимённое место без подсказки: годится, если оно в пределах
    :data:`APPROXIMATE_RADIUS_KM` от названного соседа. Иначе — сам сосед.
    """
    anchors = await resolve(session, hint)
    if not anchors:
        return []
    anchor = anchors[0]
    plain = [c for c in await resolve(session, name) if haversine_km(c.coords, anchor.coords) <= APPROXIMATE_RADIUS_KM]
    if plain:
        return [min(plain, key=lambda c: haversine_km(c.coords, anchor.coords))]
    neighbour = anchor.name.split(",")[0] or hint
    return [Candidate(lat=anchor.lat, lon=anchor.lon, name=f"{name} (рядом с {neighbour})", kind="near")]


#: Слова подсказки региона, которые ничего не говорят о месте («район», «округ», «область»…).
_HINT_STOP_WORDS = frozenset(
    {"район", "округ", "область", "край", "республика", "муниципальный", "городской", "сельский",
     "поселение", "обл"}
)


def _hint_stems(hint: str) -> list[str]:
    """Первые 5 букв значимых слов подсказки: «Зарайский округ» -> [«зарай»]."""
    words = re.findall(r"[а-я]+", hint.lower().replace("ё", "е"))
    return [word[:5] for word in words if len(word) >= 5 and word not in _HINT_STOP_WORDS]


async def _search_by_region_in_address(name: str, hint: str) -> list[Candidate]:
    """«Пенкино (Зарайский округ)»: запрос «Пенкино, Зарайский округ» Nominatim не понимает
    (у него район записан иначе), а просто «Пенкино» находит десяток деревень. Берём из них
    только те, в адресе которых действительно есть названный район или область."""
    stems = _hint_stems(hint)
    if not stems:
        return []
    wide = await _nominatim_search(name)
    return [c for c in wide if any(stem in c.name.lower().replace("ё", "е") for stem in stems)]


# --- Опечатки: нечёткий поиск (Photon) ----------------------------------------

_PHOTON_PLACES = frozenset(
    {"city", "town", "village", "hamlet", "suburb", "locality", "isolated_dwelling", "allotments"}
)
_photon_lock = asyncio.Lock()
_photon_last_at = 0.0
#: Насколько название из заявки должно быть похоже на найденное, чтобы считать его опечаткой.
#: Опечатка в одну букву у названия из семи букв («Левинка»/«Левенка») даёт 0,86 — это может
#: быть и соседняя деревня, поэтому такое не исправляем: по опыту прода порог 0,8 переименовал
#: «Левинку» и «Монастыри» в другие места. Без подтверждения регионом нужно ещё строже.
_FUZZY_RATIO_WITH_REGION = 0.88
_FUZZY_RATIO_WITHOUT_REGION = 0.9
#: Найденное нечётким поиском без региона («Кировоград» -> «Кировград» на Урале) принимаем, только
#: если оно недалеко от другого конца маршрута: так «Екатеринбург -> Кировоград» исправится,
#: а настоящий украинский Кировоград — нет.
UNVERIFIED_MAX_KM = 300
_FUZZY_MIN_NAME_LENGTH = 6


async def _photon_search(query: str) -> list[dict]:
    """Нечёткий поиск мест по OpenStreetMap (Photon): понимает опечатки и падежи."""
    global _photon_last_at
    async with _photon_lock:
        wait = 1.0 - (time.monotonic() - _photon_last_at)
        if wait > 0:
            await asyncio.sleep(wait)
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                response = await client.get(
                    settings.photon_url,
                    params={"q": query, "limit": 6, "osm_tag": "place"},
                    headers={"User-Agent": settings.geocoder_user_agent},
                )
            response.raise_for_status()
            data = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise GeocoderUnavailable(f"photon: {exc}") from exc
        finally:
            _photon_last_at = time.monotonic()
    return list(data.get("features") or [])


def _photon_candidate(feature: dict) -> Optional[Candidate]:
    props = feature.get("properties") or {}
    found = props.get("name") or ""
    try:
        lon, lat = feature["geometry"]["coordinates"][:2]
        lat, lon = float(lat), float(lon)
    except (KeyError, TypeError, ValueError):
        return None
    if props.get("osm_value") not in _PHOTON_PLACES or not found:
        return None
    parts = [found, props.get("county") or props.get("district"), props.get("state"), props.get("country")]
    return Candidate(lat=lat, lon=lon, name=", ".join(str(p) for p in parts if p)[:160], kind=props["osm_value"])


def _is_longer_name(first: str, second: str) -> bool:
    """Одно название — начало другого с хвостом от двух букв: так отличаются разные места
    (Монастыри/Монастырище), а в опечатке хвост не дописывают."""
    short, long_ = sorted((first, second), key=len)
    return len(long_) - len(short) >= 2 and long_.startswith(short)


async def _search_with_typos(name: str, hint: Optional[str]) -> list[Candidate]:
    """«Острогоржск (Воронежская обл.)»: Nominatim такого не знает, а нечёткий поиск находит
    «Острогожск». Принимаем только очень похожее название; если регион известен — он обязан
    совпасть. Найденному ставим ``canonical`` — правильное написание."""
    if len(city_key(name)) < _FUZZY_MIN_NAME_LENGTH or not settings.geocoding_fuzzy_enabled:
        return []
    stems = _hint_stems(hint) if hint else []
    features = await _photon_search(f"{name} {hint}" if hint else name)

    base = city_key(name)
    matches: list[tuple[float, str, Candidate]] = []
    for feature in features:
        props = feature.get("properties") or {}
        title = props.get("name") or ""
        found_key = city_key(title)
        if _is_longer_name(base, found_key):
            continue  # «Монастыри» и «Монастырище» — разные места, а не опечатка
        ratio = difflib.SequenceMatcher(None, base, found_key).ratio()
        area = " ".join(str(props.get(key) or "") for key in ("state", "county", "district", "city"))
        in_region = bool(stems) and any(stem in area.lower().replace("ё", "е") for stem in stems)
        if stems:
            if not in_region or ratio < _FUZZY_RATIO_WITH_REGION:
                continue
        elif ratio < _FUZZY_RATIO_WITHOUT_REGION:
            continue
        candidate = _photon_candidate(feature)
        if candidate is not None:
            matches.append((ratio, title, candidate))
    if not matches:
        return []

    # Среди похожих названий («Кондровка» и «Кондуровка» для «Кондоровка») верно одно —
    # самое похожее; остальные не берём, иначе исправление превратится в угадывание.
    best = max(ratio for ratio, _, _ in matches)
    return [
        Candidate(
            cand.lat, cand.lon, cand.name, cand.kind,
            canonical=title if city_key(title) != base else "",
            verified=bool(stems),
        )
        for ratio, title, cand in matches
        if ratio >= best - 0.02
    ]


# --- Деревни, которых нет в OSM: ближайший известный пункт ---------------------

_AREA_KINDS = frozenset({"district", "county", "state_district", "municipality", "historic"})
_REGION_ONLY_RE = re.compile(r"област|край|республик", re.IGNORECASE)
_DISTRICT_WORD_RE = re.compile(r"район|р-н|округ|\bмо\b|\bго\b|\bао\b|муницип", re.IGNORECASE)
_DISTRICT_SUFFIX_RE = re.compile(r"\b(?:мо|го|ао|округ)\b", re.IGNORECASE)
#: Центр области — слишком грубая привязка: годится, только если другой конец маршрута далеко.
REGION_GUESS_MIN_KM = 400


async def _nominatim_area(query: str) -> list[dict]:
    """Строки Nominatim как есть — для районов и областей (в них нет населённых пунктов)."""
    return await _nominatim_rows(query)


def _area_point(rows: list[dict], kinds: frozenset, stems: list[str]) -> Optional[tuple[float, float, str]]:
    for row in rows:
        kind = row.get("addresstype") or row.get("type") or ""
        head = str(row.get("display_name") or "").split(",")[0]
        if kind not in kinds:
            continue
        if stems and not any(stem in head.lower().replace("ё", "е") for stem in stems):
            continue
        try:
            return float(row["lat"]), float(row["lon"]), head
        except (KeyError, TypeError, ValueError):
            continue
    return None


async def _approximate_by_area(name: str, hint: str) -> list[Candidate]:
    """«Коробки (район Каховки)», «Пенкино (Зарайский округ)», «Вершины (Запорожская обл.)»:
    деревни в картах нет, но район или область названы — считаем от их центра.

    Район даёт погрешность в десятки километров, область — до сотни и больше, поэтому
    центр области годится лишь для далёкого маршрута (см. :data:`REGION_GUESS_MIN_KM`)."""
    stems = _hint_stems(hint)
    if _REGION_ONLY_RE.search(hint) and not _DISTRICT_WORD_RE.search(hint):
        point = _area_point(await _nominatim_area(hint), frozenset({"state"}), [])
        if point is None:
            return []
        return [Candidate(point[0], point[1], f"{name} (область: {point[2]})", kind="region")]

    if not stems:
        return []
    district = _DISTRICT_SUFFIX_RE.sub("район", hint)
    point = _area_point(await _nominatim_area(district), _AREA_KINDS, stems)
    if point is None and settings.geocoding_fuzzy_enabled:
        # «Зарайский округ» -> Зарайск, «район Каховки» -> Каховка: административного района в
        # картах нет, зато есть его центр, и нечёткий поиск находит его по основе названия.
        skip = _HINT_STOP_WORDS | {"мо", "го", "ао"}
        words = [w for w in re.findall(r"[А-Яа-яЁё\-]+", hint) if w.lower() not in skip]
        for feature in await _photon_search(" ".join(words)):
            candidate = _photon_candidate(feature)
            if candidate and any(candidate.name.lower().replace("ё", "е").startswith(stem) for stem in stems):
                point = (candidate.lat, candidate.lon, candidate.name.split(",")[0])
                break
    if point is None:
        return []
    return [Candidate(point[0], point[1], f"{name} (рядом с {point[2]})", kind="near")]


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
    if not found and hint and _is_region_hint(hint):
        found = await _search_by_region_in_address(name, hint)
    if not found and hint and not _is_region_hint(hint):
        found = await _approximate_by_hint(session, name, hint)

    # Не нашли — возможно, опечатка; затем — привязка к ближайшему известному пункту.
    # Сбой нечёткого поиска (сеть) не должен записать «не найдено» на неделю.
    fuzzy_unavailable = False
    if not found:
        try:
            found = await _search_with_typos(name, hint)
        except GeocoderUnavailable as exc:
            log.warning("Нечёткий поиск недоступен: %s", exc)
            fuzzy_unavailable = True
    if not found and hint and not fuzzy_unavailable:
        try:
            found = await _approximate_by_area(name, hint)
        except GeocoderUnavailable as exc:
            log.warning("Поиск района недоступен: %s", exc)
            fuzzy_unavailable = True
    if not found and fuzzy_unavailable:
        return []

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
