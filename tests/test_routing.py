"""Расстояние по дорогам: ответ OSRM, фоновый воркер, показ на сайте.

В сеть тесты не ходят: ``routing.road_distance_km`` подменяется заглушкой.
"""

import pytest
from sqlalchemy import select

from app import routing
from app.db.base import SessionLocal
from app.models import Order
from app.search import refresh_derived
from app.services import routes
from app.web import presenters

PETROZAVODSK = (61.785, 34.346)
SORTAVALA = (61.703, 30.691)
STRAIGHT_KM = 197  # прямая между ними, для проверки отсева


async def _reload(order_id: int) -> Order:
    async with SessionLocal() as fresh:
        return (await fresh.execute(select(Order).where(Order.id == order_id))).scalar_one()


def _ok(meters: float) -> dict:
    return {"code": "Ok", "routes": [{"distance": meters}]}


# --- Разбор ответа ------------------------------------------------------------


def test_parse_distance_converts_meters_to_km():
    assert routing.parse_distance_km(_ok(243_456), PETROZAVODSK, SORTAVALA) == 243.5


def test_parse_distance_no_route_is_none():
    assert routing.parse_distance_km({"code": "NoRoute"}, PETROZAVODSK, SORTAVALA) is None


def test_parse_distance_garbage_is_none():
    assert routing.parse_distance_km({"code": "Ok", "routes": []}, PETROZAVODSK, SORTAVALA) is None
    assert routing.parse_distance_km({"code": "Ok", "routes": [{"distance": "x"}]}, PETROZAVODSK, SORTAVALA) is None


def test_parse_distance_rejects_road_much_shorter_than_straight_line():
    """Дорога не бывает короче прямой — такой ответ неверный."""
    assert routing.parse_distance_km(_ok(50_000), PETROZAVODSK, SORTAVALA) is None


# --- Воркер -------------------------------------------------------------------


@pytest.fixture
def fake_osrm(monkeypatch):
    calls: list[tuple] = []

    async def fake(origin, destination, via=()):
        calls.append((origin, destination))
        return 243.5

    monkeypatch.setattr(routing, "road_distance_km", fake)
    return calls


async def test_worker_fills_distance_for_geocoded_orders(make_order, fake_osrm):
    order = await make_order(from_coords=PETROZAVODSK, to_coords=SORTAVALA)

    done = await routes.route_pending()

    assert done == 1
    fresh = await _reload(order.id)
    assert fresh.distance_km == 243.5
    assert fresh.route_checked_at is not None


async def test_worker_skips_orders_without_coordinates(make_order, fake_osrm):
    order = await make_order()  # геокодер ещё не отработал

    assert await routes.route_pending() == 0
    assert fake_osrm == []
    assert (await _reload(order.id)).route_checked_at is None


async def test_same_pair_of_points_is_requested_only_once(make_order, fake_osrm):
    first = await make_order(from_coords=PETROZAVODSK, to_coords=SORTAVALA)
    await routes.route_pending()
    second = await make_order(from_coords=PETROZAVODSK, to_coords=SORTAVALA)

    await routes.route_pending()

    assert len(fake_osrm) == 1
    assert (await _reload(second.id)).distance_km == (await _reload(first.id)).distance_km == 243.5


async def test_unavailable_service_leaves_order_for_a_retry(make_order, monkeypatch):
    async def down(origin, destination, via=()):
        raise routing.RoutingUnavailable("HTTP 429")

    monkeypatch.setattr(routing, "road_distance_km", down)
    order = await make_order(from_coords=PETROZAVODSK, to_coords=SORTAVALA)

    with pytest.raises(routing.RoutingUnavailable):
        await routes.route_pending()

    fresh = await _reload(order.id)
    assert fresh.route_checked_at is None and fresh.distance_km is None


async def test_no_route_marks_order_checked_without_distance(make_order, monkeypatch):
    async def nothing(origin, destination, via=()):
        return None

    monkeypatch.setattr(routing, "road_distance_km", nothing)
    order = await make_order(from_coords=PETROZAVODSK, to_coords=SORTAVALA)

    await routes.route_pending()
    await routes.route_pending()  # повторно его не берём

    fresh = await _reload(order.id)
    assert fresh.distance_km is None and fresh.route_checked_at is not None


async def test_same_point_needs_no_request(make_order, fake_osrm):
    order = await make_order(from_coords=PETROZAVODSK, to_coords=PETROZAVODSK)

    await routes.route_pending()

    assert fake_osrm == []
    assert (await _reload(order.id)).route_checked_at is not None


async def test_changing_a_city_resets_the_distance(make_order, session, fake_osrm):
    order = await make_order(from_coords=PETROZAVODSK, to_coords=SORTAVALA)
    await routes.route_pending()
    order = await _reload(order.id)
    assert order.distance_km == 243.5

    session.add(order)
    order.to_city = "Лахденпохья"
    refresh_derived(order)

    assert order.distance_km is None and order.route_checked_at is None
    assert order.from_lat is None  # координаты тоже пересчитает геокодер


# --- Показ ----------------------------------------------------------------------


def test_distance_label():
    order = Order(distance_km=243.4)
    assert presenters.distance_label(order) == "243 км"
    assert presenters.distance_label(Order(distance_km=None)) == ""
    assert presenters.distance_label(Order(distance_km=0.4)) == ""


def test_price_per_km_label():
    assert presenters.price_per_km_label(Order(distance_km=200.0, client_price=8000)) == "40 ₽/км"
    assert presenters.price_per_km_label(Order(distance_km=243.5, client_price=8000)) == "33 ₽/км"
    assert presenters.price_per_km_label(Order(distance_km=None, client_price=8000)) == ""
    assert presenters.price_per_km_label(Order(distance_km=200.0, client_price=None)) == ""
    assert presenters.price_per_km_label(Order(distance_km=0.4, client_price=8000)) == ""


async def test_site_shows_distance_on_feed_and_card(client, make_order, session):
    order = await make_order(from_coords=PETROZAVODSK, to_coords=SORTAVALA)
    order.distance_km = 243.5
    await session.commit()

    feed = await client.get("/")
    card = await client.get(f"/orders/{order.id}")

    for page in (feed, card):
        assert "Расстояние: 244 км · 57 ₽/км" in page.text  # цена заказа по умолчанию 14000


async def test_site_without_distance_shows_nothing(client, make_order):
    order = await make_order()

    assert "Расстояние:" not in (await client.get("/")).text
    assert "Расстояние:" not in (await client.get(f"/orders/{order.id}")).text


async def test_geocoding_again_resets_the_distance(make_order, session, fake_osrm):
    """Координаты поменялись (например, поправили место вручную) — километры считаем заново."""
    from app.services.geocode import geocode_pending

    order = await make_order(from_coords=PETROZAVODSK, to_coords=SORTAVALA)
    await routes.route_pending()
    async with SessionLocal() as fresh:
        row = await fresh.get(Order, order.id)
        row.geo_checked_at = None
        await fresh.commit()

    await geocode_pending()

    again = await _reload(order.id)
    assert again.distance_km is None and again.route_checked_at is None
