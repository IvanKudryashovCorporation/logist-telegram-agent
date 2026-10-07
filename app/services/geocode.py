"""Фоновый геокодер: проставляет заказам координаты для фильтра радиуса.

Работает в процессе агента, а не в обработчике сообщения: сеть (Nominatim)
не должна задерживать разбор заявки, а при её недоступности заказ всё равно
создаётся — просто без координат, и ищется по названию.

Тот же воркер догеокодирует уже существующие заказы: после миграции у них
``geo_checked_at IS NULL``.
"""

import asyncio
import logging
import re
from typing import Optional

from sqlalchemy import and_, or_, select, update
from sqlalchemy.orm.attributes import flag_modified

from app import geo
from app.config import settings
from app.db.base import SessionLocal
from app.models import HIDDEN_STATUSES, ActionLog, ActorType, Order
from app.search import refresh_derived
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
                    .where(
                        or_(
                            Order.geo_checked_at.is_(None),
                            # Обработан старой логикой — пересчитываем новой.
                            Order.geo_version.is_(None),
                            Order.geo_version < geo.GEO_VERSION,
                            # Без координат и проверен до последнего улучшения поиска:
                            # возможно, теперь место найдётся.
                            and_(
                                or_(Order.from_lat.is_(None), Order.to_lat.is_(None)),
                                Order.geo_checked_at < geo.GEO_LOGIC_DATE,
                            ),
                        )
                    )
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


def _corrected_city(raw_city: Optional[str], candidate: Optional[geo.Candidate]) -> Optional[str]:
    """Город с исправленной опечаткой («Острогоржск (Воронежская обл.)» -> «Острогожск (…)»).

    Меняется только название, всё остальное написанное диспетчером (регион в скобках,
    приставки «с.», «п.») остаётся как есть. ``None`` — менять нечего."""
    if not raw_city or candidate is None or not candidate.canonical:
        return None
    name, _ = geo.normalize_place(raw_city)
    pattern = re.compile(re.escape(name), re.IGNORECASE) if name else None
    if pattern is None or not pattern.search(raw_city):
        return None
    fixed = pattern.sub(candidate.canonical, raw_city, count=1)
    return fixed if fixed != raw_city else None


def _drop_unverified_guess(
    chosen: Optional[geo.Candidate], other: Optional[geo.Candidate]
) -> Optional[geo.Candidate]:
    """Название без региона, найденное нечётким поиском, проверяем по другому концу маршрута."""
    if chosen is None or chosen.verified:
        return chosen
    if other is None or geo.haversine_km(chosen.coords, other.coords) > geo.UNVERIFIED_MAX_KM:
        return None
    return chosen


def _drop_rough_guess(chosen: Optional[geo.Candidate], other: Optional[geo.Candidate]) -> Optional[geo.Candidate]:
    """Центр области вместо деревни — грубая привязка (до сотни километров и больше).

    Для далёкого маршрута это незаметная погрешность, а для поездки внутри области —
    ложь: от Москвы до «центра Московской области» нет ни километра. Поэтому такой
    привязке верим, только когда другой конец маршрута далеко."""
    if chosen is None or chosen.kind != "region":
        return chosen
    if other is None or geo.haversine_km(chosen.coords, other.coords) < geo.REGION_GUESS_MIN_KM:
        return None
    return chosen


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
        session, origin, order.from_address,
        near=destination.coords if destination else None, city=order.from_city,
        raw_text=order.raw_text,
    )
    destination = await geo.refine_by_address(
        session, destination, order.to_address,
        near=origin.coords if origin else None, city=order.to_city,
        raw_text=order.raw_text,
    )

    origin, destination = _drop_rough_guess(origin, destination), _drop_rough_guess(destination, origin)
    origin, destination = (
        _drop_unverified_guess(origin, destination),
        _drop_unverified_guess(destination, origin),
    )

    # Опечатка в названии: пишем город правильно, чтобы он правильно показывался, искался
    # и склеивался с повторами. refresh_derived заодно пересчитает ключи поиска.
    fixes = {
        field: fixed
        for field, fixed in (
            ("from_city", _corrected_city(order.from_city, origin)),
            ("to_city", _corrected_city(order.to_city, destination)),
        )
        if fixed
    }
    for field, fixed in fixes.items():
        session.add(
            ActionLog(
                order_id=order.id, actor=ActorType.SYSTEM, action="city_corrected",
                details=f"{getattr(order, field)} -> {fixed}",
            )
        )
        log.info("Заказ #%s: «%s» -> «%s»", order.id, getattr(order, field), fixed)
        setattr(order, field, fixed)
    if fixes:
        refresh_derived(order)
        # Исправление названия не «освежает» заказ в админке, как и само геокодирование.
        flag_modified(order, "updated_at")
        await session.flush()

    await session.execute(
        update(Order)
        .where(Order.id == order.id)
        .values(
            from_lat=origin.lat if origin else None,
            from_lon=origin.lon if origin else None,
            to_lat=destination.lat if destination else None,
            to_lon=destination.lon if destination else None,
            geo_checked_at=now_utc_naive(),
            geo_version=geo.GEO_VERSION,
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
