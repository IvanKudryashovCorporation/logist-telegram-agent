"""Фоновый расчёт расстояния по дорогам для заказов.

Идёт после геокодера: нужны координаты обоих концов. Для той же пары точек
расстояние берётся из уже посчитанного заказа — повторяющиеся маршруты
(Петрозаводск → Сортавала и т.п.) не нагружают публичный OSRM.
"""

import asyncio
import logging
from typing import Optional

from sqlalchemy import select, update

from app import geo, plausibility, routing
from app.config import settings
from app.db.base import SessionLocal
from app.models import HIDDEN_STATUSES, Order
from app.timeutil import now_utc_naive

log = logging.getLogger("app.routes")

#: Пауза после сбоя OSRM, секунд. Не долбим сервис каждые 30 секунд.
_UNAVAILABLE_BACKOFF = 300


async def route_pending(*, limit: Optional[int] = None) -> int:
    """Один проход: считает расстояние до ``limit`` заказов. Возвращает число обработанных.

    При недоступности OSRM останавливается, не помечая текущий заказ проверенным,
    и бросает :class:`app.routing.RoutingUnavailable`.
    """
    batch = limit or settings.routing_batch
    processed = 0
    async with SessionLocal() as session:
        orders = list(
            (
                await session.execute(
                    select(Order)
                    .where(
                        Order.route_checked_at.is_(None),
                        Order.from_lat.is_not(None),
                        Order.from_lon.is_not(None),
                        Order.to_lat.is_not(None),
                        Order.to_lon.is_not(None),
                    )
                    # Живые заказы раньше скрытых: ленте нужны именно они.
                    .order_by(Order.status.in_(HIDDEN_STATUSES), Order.id.desc())
                    .limit(batch)
                )
            ).scalars().all()
        )
        for order in orders:
            await _route_order(session, order)
            processed += 1
    return processed


async def _known_distance(session, order: Order) -> Optional[float]:
    """Расстояние из другого заказа с теми же координатами (None — такого нет).

    Донор — только заказ, обработанный текущей версией геокодера: иначе во время
    пересчёта берётся устаревшее (и возможно неверное) число из ещё не обновлённого.
    """
    return (
        await session.execute(
            select(Order.distance_km)
            .where(
                Order.id != order.id,
                Order.distance_km.is_not(None),
                Order.geo_version == geo.GEO_VERSION,
                Order.from_lat == order.from_lat,
                Order.from_lon == order.from_lon,
                Order.to_lat == order.to_lat,
                Order.to_lon == order.to_lon,
            )
            .limit(1)
        )
    ).scalar_one_or_none()


async def _better_pair(session, order: Order, distance: float):
    """Альтернативная пара мест, при которой цена за км правдоподобна (или ``None``).

    Нереальная цена за км (30 800 ₽ за 26 км) почти всегда значит, что одно из названий
    найдено не там: «Жуковский» бывает и в Подмосковье, и в Брянской области. Среди
    найденных вариантов берём пару, при которой цена за километр ближе всего к обычной.
    Годится только когда у названий есть из чего выбирать.
    """
    price = float(order.client_price or 0)
    if price <= 0 or not distance:
        return None
    rate = price / distance
    if plausibility.MIN_RATE <= rate <= plausibility.MAX_RATE:
        return None
    try:
        from_candidates = await geo.resolve(session, order.from_city)
        to_candidates = await geo.resolve(session, order.to_city)
    except geo.GeocoderUnavailable:
        return None
    return plausibility.choose_pair(price, from_candidates, to_candidates)


async def _route_order(session, order: Order) -> None:
    origin = (order.from_lat, order.from_lon)
    destination = (order.to_lat, order.to_lon)
    order_id = order.id

    distance = await _known_distance(session, order)
    if distance is not None:
        # Неправдоподобное число из другого заказа не берём — спросим OSRM заново.
        checked = routing.check_km(distance, origin, destination)
        if checked is None or abs(checked - distance) > 0.05:
            distance = None
    if distance is None and origin != destination:
        distance = await routing.road_distance_km(origin, destination)

    # Диспетчер сам написал километраж («Расстояние: 70 км»). Если наш расчёт сильно
    # расходится или его нет — верим написанному: чаще всего это значит, что одно из
    # мест найдено не там (однофамилец в другом регионе).
    stated = routing.parse_stated_km(order.raw_text)
    if (
        stated
        and origin != destination
        and (distance is None or abs(distance - stated) / stated > routing.STATED_TOLERANCE)
    ):
        distance = stated

    # Цена за км нереальна — возможно, место найдено не то. Пробуем другую пару мест.
    new_coords = {}
    if distance and not stated and origin != destination:
        pair = await _better_pair(session, order, distance)
        if pair is not None:
            alt_distance = await routing.road_distance_km(pair[0].coords, pair[1].coords)
            if alt_distance and plausibility.is_plausible(float(order.client_price) / alt_distance):
                log.info(
                    "Заказ #%s: цена за км была нереальной (%.0f км), пара мест заменена, стало %.0f км",
                    order_id, distance, alt_distance,
                )
                distance = alt_distance
                new_coords = {
                    "from_lat": pair[0].lat, "from_lon": pair[0].lon,
                    "to_lat": pair[1].lat, "to_lon": pair[1].lon,
                }

    await session.execute(
        update(Order)
        .where(Order.id == order_id)
        .values(
            distance_km=distance,
            route_checked_at=now_utc_naive(),
            **new_coords,
            # Явное значение отключает onupdate: расчёт километров не должен
            # «освежать» заказ в админке, сортирующей по updated_at.
            updated_at=Order.updated_at,
        )
        .execution_options(synchronize_session=False)
    )
    await session.commit()


async def heal_stale_routes() -> int:
    """Сбрасывает расстояния, которые не проходят проверку на здравый смысл.

    Страховка на случай, когда неверное число уже сохранилось (и разошлось по заказам
    с теми же точками): воркер пересчитает их. Заказы, где расстояние взято из текста
    диспетчера, не трогаем — оно намеренно может расходиться с расчётом.
    Возвращает, сколько заказов отправлено на пересчёт.
    """
    async with SessionLocal() as session:
        rows = (
            await session.execute(
                select(
                    Order.id, Order.from_lat, Order.from_lon, Order.to_lat, Order.to_lon,
                    Order.distance_km, Order.raw_text,
                ).where(
                    Order.distance_km.is_not(None),
                    Order.from_lat.is_not(None),
                    Order.to_lat.is_not(None),
                )
            )
        ).all()
        bad = []
        for order_id, flat, flon, tlat, tlon, km, raw_text in rows:
            if (flat, flon) == (tlat, tlon) or routing.parse_stated_km(raw_text):
                continue
            checked = routing.check_km(km, (flat, flon), (tlat, tlon))
            if checked is None or abs(checked - km) > 0.05:
                bad.append(order_id)
        if bad:
            await session.execute(
                update(Order)
                .where(Order.id.in_(bad))
                .values(distance_km=None, route_checked_at=None, updated_at=Order.updated_at)
                .execution_options(synchronize_session=False)
            )
            await session.commit()
            log.warning("Расстояния не прошли проверку, пересчитываю: %s", bad)
        return len(bad)


async def run_routing_worker(stop_event: asyncio.Event) -> None:
    """Фоновый цикл расчёта расстояний, переживает любые ошибки."""
    if not (settings.routing_enabled and settings.geocoding_enabled):
        log.info("Расчёт расстояний выключен (ROUTING_ENABLED=false или геокодирование выключено)")
        return

    try:
        await heal_stale_routes()
    except Exception:
        log.exception("Не удалось проверить сохранённые расстояния")

    interval = max(5, settings.routing_poll_seconds)
    log.info("Расчёт расстояний по дорогам запущен: проход каждые %s с, сервер %s", interval, settings.routing_url)
    while not stop_event.is_set():
        delay = interval
        try:
            done = await route_pending()
            if done:
                log.info("Посчитано расстояний: %s", done)
                delay = 1  # есть что догонять — следующий проход сразу (темп держит routing)
        except routing.RoutingUnavailable as exc:
            log.warning("OSRM недоступен, повтор через %s с: %s", _UNAVAILABLE_BACKOFF, exc)
            delay = _UNAVAILABLE_BACKOFF
        except Exception:
            log.exception("Ошибка расчёта расстояний")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=delay)
        except asyncio.TimeoutError:
            continue
