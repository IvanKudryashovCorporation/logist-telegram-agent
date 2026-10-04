"""Расстояние по дорогам между двумя точками — через OSRM.

OSRM строит маршрут по дорожному графу OpenStreetMap, поэтому ответ — это
километры, которые реально проедет машина, а не прямая линия (см.
:func:`app.geo.haversine_km`). По умолчанию используется публичный демо-сервер
``router.project-osrm.org``: он бесплатный и без ключа, но без гарантий и с
просьбой не превышать ~1 запрос в секунду. Поэтому:

* запросы идут только из фонового воркера (:mod:`app.services.routes`), веб не ждёт сеть;
* между запросами пауза, результат хранится в заказе и переиспользуется;
* недоступность сервиса — не ошибка заказа: километры просто появятся позже.
"""

import asyncio
import logging
import re
import time
from typing import Optional

import httpx

from app.config import settings
from app.geo import Coords, haversine_km

log = logging.getLogger("app.routing")

#: Пауза между запросами к публичному серверу, секунд.
_MIN_INTERVAL = 1.1
#: Дорога не бывает короче прямой; чуть ниже — допуск на неточность координат.
_MIN_RATIO = 0.9

#: Дорога длиннее прямой больше чем в 4 раза (и больше чем на 100 км) — это ошибка графа.
_MAX_RATIO = 4.0
_MAX_EXTRA_KM = 100.0
#: Для короткой поездки при сбое графа считаем «по прямой с запасом на извилистость».
_SHORT_TRIP_KM = 100.0
_DETOUR = 1.3

#: Диспетчеры часто сами пишут километраж («Расстояние: 70 км», «🗺 382 км», «395 км»).
_STATED_EXPLICIT_RE = re.compile(
    r"(?:расстояни[ея]|дистанци[яи]|протяж[её]нность|🗺)\D{0,12}?(\d{1,4}(?:[.,]\d+)?)\s*км(?![\wа-яё/])",
    re.IGNORECASE,
)
_STATED_PLAIN_RE = re.compile(r"(?<![\d.,])(\d{2,4})\s*км(?![\wа-яё/])", re.IGNORECASE)
#: Насколько расчёт может расходиться с написанным, прежде чем верим написанному.
STATED_TOLERANCE = 0.4

_lock = asyncio.Lock()
_last_request_at = 0.0


class RoutingUnavailable(Exception):
    """Сеть/OSRM недоступны — заказ НЕ помечаем проверенным, попробуем позже."""


async def road_distance_km(origin: Coords, destination: Coords) -> Optional[float]:
    """Длина маршрута по дорогам, км. ``None`` — маршрута нет (море, нет дороги).

    Бросает :class:`RoutingUnavailable` при сбое сети или ответе сервера 429/5xx.
    """
    global _last_request_at
    url = (
        f"{settings.routing_url.rstrip('/')}/route/v1/driving/"
        f"{origin[1]:.6f},{origin[0]:.6f};{destination[1]:.6f},{destination[0]:.6f}"
    )
    async with _lock:
        wait = _MIN_INTERVAL - (time.monotonic() - _last_request_at)
        if wait > 0:
            await asyncio.sleep(wait)
        try:
            async with httpx.AsyncClient(timeout=20, trust_env=False) as client:
                response = await client.get(
                    url,
                    params={"overview": "false", "alternatives": "false", "steps": "false"},
                    headers={"User-Agent": settings.geocoder_user_agent},
                )
        except httpx.HTTPError as exc:
            raise RoutingUnavailable(type(exc).__name__) from exc
        finally:
            _last_request_at = time.monotonic()

    if response.status_code == 429 or response.status_code >= 500:
        raise RoutingUnavailable(f"HTTP {response.status_code}")
    try:
        payload = response.json()
    except ValueError as exc:
        raise RoutingUnavailable("не JSON в ответе") from exc
    return parse_distance_km(payload, origin, destination)


def parse_stated_km(text: Optional[str]) -> Optional[float]:
    """Километраж, который диспетчер написал в заявке. ``None`` — не написал или неясно.

    Явное «Расстояние: 70 км» / «🗺 382 км» берём сразу; голое «395 км» — только если
    оно единственное в тексте (иначе это может быть «+20 км за город»).
    """
    if not text:
        return None
    explicit = _STATED_EXPLICIT_RE.search(text)
    if explicit:
        value = float(explicit.group(1).replace(",", "."))
    else:
        plain = _STATED_PLAIN_RE.findall(text)
        if len(plain) != 1:
            return None
        value = float(plain[0])
    return value if 5 <= value <= 5000 else None


def parse_distance_km(payload: dict, origin: Coords, destination: Coords) -> Optional[float]:
    """Достаёт километры из ответа OSRM и отбрасывает заведомо неверные."""
    if payload.get("code") != "Ok":
        # NoRoute / NoSegment / InvalidQuery — для этой пары точек дороги нет.
        return None
    try:
        meters = float(payload["routes"][0]["distance"])
    except (KeyError, IndexError, TypeError, ValueError):
        return None
    return check_km(meters / 1000, origin, destination)


def check_km(km: float, origin: Coords, destination: Coords) -> Optional[float]:
    """Проверяет километраж на здравый смысл относительно прямой между точками.

    Используется и для свежего ответа OSRM, и для расстояния, взятого из другого
    заказа с теми же точками: неверное число иначе перекочёвывает от заказа к заказу.
    """
    straight = haversine_km(origin, destination)
    if km < straight * _MIN_RATIO:
        log.warning("OSRM вернул %.0f км при прямой %.0f км — отбрасываю", km, straight)
        return None
    if km > straight * _MAX_RATIO + _MAX_EXTRA_KM:
        # Граф дорог «разорван» (граница, паром): Алахадзы → Гагра в 9 км по прямой
        # OSRM вёл через полмира, 3554 км. Для короткой поездки берём прямую с запасом,
        # для длинной — лучше без расстояния, чем с неверным.
        log.warning("OSRM вернул %.0f км при прямой %.0f км — маршрут нереальный", km, straight)
        return round(straight * _DETOUR, 1) if straight < _SHORT_TRIP_KM else None
    return round(km, 1)
