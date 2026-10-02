"""Фоновый расчёт расстояния по дорогам для заказов.

Идёт после геокодера: нужны координаты обоих концов. Для той же пары точек
расстояние берётся из уже посчитанного заказа — повторяющиеся маршруты
(Петрозаводск → Сортавала и т.п.) не нагружают публичный OSRM.
"""

import asyncio
import logging
from typing import Optional

from sqlalchemy import select, update

from app import routing
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
    """Расстояние из другого заказа с теми же координатами (None — такого нет)."""
    return (
        await session.execute(
            select(Order.distance_km)
            .where(
                Order.id != order.id,
                Order.distance_km.is_not(None),
                Order.from_lat == order.from_lat,
                Order.from_lon == order.from_lon,
                Order.to_lat == order.to_lat,
                Order.to_lon == order.to_lon,
            )
            .limit(1)
        )
    ).scalar_one_or_none()


async def _route_order(session, order: Order) -> None:
    origin = (order.from_lat, order.from_lon)
    destination = (order.to_lat, order.to_lon)
    order_id = order.id

    distance = await _known_distance(session, order)
    if distance is None and origin != destination:
        distance = await routing.road_distance_km(origin, destination)

    await session.execute(
        update(Order)
        .where(Order.id == order_id)
        .values(
            distance_km=distance,
            route_checked_at=now_utc_naive(),
            # Явное значение отключает onupdate: расчёт километров не должен
            # «освежать» заказ в админке, сортирующей по updated_at.
            updated_at=Order.updated_at,
        )
        .execution_options(synchronize_session=False)
    )
    await session.commit()


async def run_routing_worker(stop_event: asyncio.Event) -> None:
    """Фоновый цикл расчёта расстояний, переживает любые ошибки."""
    if not (settings.routing_enabled and settings.geocoding_enabled):
        log.info("Расчёт расстояний выключен (ROUTING_ENABLED=false или геокодирование выключено)")
        return

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
