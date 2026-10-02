"""Фоновый геокодер: проставляет заказам координаты для фильтра радиуса.

Работает в процессе агента, а не в обработчике сообщения: сеть (Nominatim)
не должна задерживать разбор заявки, а при её недоступности заказ всё равно
создаётся — просто без координат, и ищется по названию.

Тот же воркер догеокодирует уже существующие заказы: после миграции у них
``geo_checked_at IS NULL``.
"""

import asyncio
import logging
from typing import Optional

from sqlalchemy import select, update

from app import geo
from app.config import settings
from app.db.base import SessionLocal
from app.models import HIDDEN_STATUSES, Order
from app.timeutil import now_utc_naive

log = logging.getLogger("app.geocode")

#: Пауза после сбоя сети/Nominatim, секунд. Не долбим сервис каждые 30 секунд.
_UNAVAILABLE_BACKOFF = 300


async def geocode_pending(*, limit: Optional[int] = None) -> int:
    """Один проход: геокодирует до ``limit`` заказов без координат.

    Возвращает число обработанных заказов. При недоступности геокодера
    останавливается (не помечая текущий заказ проверенным) и бросает
    :class:`app.geo.GeocoderUnavailable` — вызывающий решает, когда повторять.
    """
    batch = limit or settings.geocode_batch
    processed = 0
    async with SessionLocal() as session:
        orders = list(
            (
                await session.execute(
                    select(Order)
                    .where(Order.geo_checked_at.is_(None))
                    # Живые заказы раньше скрытых: ленте нужны именно они.
                    .order_by(Order.status.in_(HIDDEN_STATUSES), Order.id.desc())
                    .limit(batch)
                )
            ).scalars().all()
        )
        for order in orders:
            await _geocode_order(session, order)
            processed += 1
    return processed


async def _geocode_order(session, order: Order) -> None:
    from_candidates = await geo.resolve(session, order.from_city)
    to_candidates = await geo.resolve(session, order.to_city)

    # Другой конец маршрута помогает выбрать между одноимёнными местами, но
    # только когда он сам однозначен.
    near_from = to_candidates[0].coords if len(to_candidates) == 1 else None
    near_to = from_candidates[0].coords if len(from_candidates) == 1 else None
    origin = geo.pick(from_candidates, near_from)
    destination = geo.pick(to_candidates, near_to)

    # Деревню, найденную не там, поправляет адрес из заявки («Мрия», «Оползневое»).
    origin = await geo.refine_by_address(
        session, origin, order.from_address, near=destination.coords if destination else None
    )
    destination = await geo.refine_by_address(
        session, destination, order.to_address, near=origin.coords if origin else None
    )

    await session.execute(
        update(Order)
        .where(Order.id == order.id)
        .values(
            from_lat=origin.lat if origin else None,
            from_lon=origin.lon if origin else None,
            to_lat=destination.lat if destination else None,
            to_lon=destination.lon if destination else None,
            geo_checked_at=now_utc_naive(),
            # Координаты поменялись — расстояние по ним устарело, пусть пересчитается.
            distance_km=None,
            route_checked_at=None,
            # Явное значение отключает onupdate: геокодирование не должно
            # «освежать» заказ в админке, сортирующей по updated_at.
            updated_at=Order.updated_at,
        )
        .execution_options(synchronize_session=False)
    )
    await session.commit()


async def run_geocode_worker(stop_event: asyncio.Event) -> None:
    """Фоновый цикл геокодирования, переживает любые ошибки."""
    if not settings.geocoding_enabled:
        log.info("Геокодирование выключено (GEOCODING_ENABLED=false)")
        return

    interval = max(5, settings.geocode_poll_seconds)
    log.info("Геокодер запущен: проход каждые %s с, пачка %s", interval, settings.geocode_batch)
    while not stop_event.is_set():
        delay = interval
        try:
            done = await geocode_pending()
            if done:
                log.info("Геокодировано заказов: %s", done)
                # Есть что догонять (бэкфилл) — следующий проход сразу: темп
                # всё равно ограничен 1 запросом/сек внутри геокодера.
                delay = 1
        except geo.GeocoderUnavailable as exc:
            log.warning("Геокодер недоступен, повтор через %s с: %s", _UNAVAILABLE_BACKOFF, exc)
            delay = _UNAVAILABLE_BACKOFF
        except Exception:
            log.exception("Ошибка геокодера")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=delay)
        except asyncio.TimeoutError:
            continue
