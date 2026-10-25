"""Сортировка ленты: по расстоянию и цене за км, в обе стороны."""

import re
from datetime import timedelta
from decimal import Decimal

import pytest

from app.timeutil import now_msk_naive
from app.web import queries


async def _order(make_order, session, *, price, km, **kwargs):
    kwargs.setdefault("pickup_at", now_msk_naive() + timedelta(days=1))
    order = await make_order(price=price, **kwargs)
    order.distance_km = km
    order.pickup_asap = order.pickup_at is None  # как делает разбор заявки
    await session.commit()
    return order.id


async def _ids(session, sort, direction=""):
    page = await queries.fetch_feed(session, sort=sort, direction=direction, page=1, page_size=50)
    return [order.id for order in page.items]


@pytest.fixture
async def trio(make_order, session):
    """100 км по 80 ₽/км, 200 км по 40 ₽/км, 50 км по 100 ₽/км."""
    return {
        "a": await _order(make_order, session, price="8000", km=100.0),
        "b": await _order(make_order, session, price="8000", km=200.0),
        "c": await _order(make_order, session, price="5000", km=50.0),
    }


async def test_sort_by_trip_distance_both_directions(session, trio):
    assert await _ids(session, "trip_km", "desc") == [trio["b"], trio["a"], trio["c"]]
    assert await _ids(session, "trip_km", "asc") == [trio["c"], trio["a"], trio["b"]]


async def test_sort_by_price_per_km_both_directions(session, trio):
    assert await _ids(session, "per_km", "desc") == [trio["c"], trio["a"], trio["b"]]
    assert await _ids(session, "per_km", "asc") == [trio["b"], trio["a"], trio["c"]]


async def test_sort_by_price_both_directions(make_order, session):
    cheap = await _order(make_order, session, price="3000", km=100.0)
    mid = await _order(make_order, session, price="8000", km=100.0)
    dear = await _order(make_order, session, price="20000", km=100.0)

    assert await _ids(session, "price", "desc") == [dear, mid, cheap]
    assert await _ids(session, "price", "asc") == [cheap, mid, dear]


async def test_orders_without_value_are_last_in_both_directions(make_order, session, trio):
    no_km = await _order(make_order, session, price="9000", km=None)
    no_price = await _order(make_order, session, price="", km=120.0)

    for sort in ("trip_km", "per_km"):
        for direction in ("asc", "desc"):
            ids = await _ids(session, sort, direction)
            assert set(ids[-2:] if sort == "per_km" else ids[-1:]) <= {no_km, no_price}, (sort, direction)

    # по цене: заказ без цены в конце при любом направлении
    for direction in ("asc", "desc"):
        assert (await _ids(session, "price", direction))[-1] == no_price


async def test_per_km_ignores_tiny_distance(make_order, session):
    """Менее километра — цена за км не считается (иначе деление на почти ноль)."""
    tiny = await _order(make_order, session, price="3000", km=0.4)
    normal = await _order(make_order, session, price="3000", km=100.0)

    assert await _ids(session, "per_km", "desc") == [normal, tiny]
    assert await _ids(session, "per_km", "asc") == [normal, tiny]


async def test_default_direction_keeps_old_behaviour(session, trio):
    assert queries.effective_direction("price") == "desc"
    assert queries.effective_direction("date") == "asc"
    assert queries.effective_direction("recent") == "desc"
    assert queries.effective_direction("price", "мусор") == "desc"
    assert queries.effective_direction("price", "ASC") == "asc"


async def test_recent_and_date_both_directions(make_order, session):
    base = now_msk_naive() + timedelta(days=1)
    early = await _order(make_order, session, price="1000", km=10.0, pickup_at=base)
    late = await _order(make_order, session, price="1000", km=10.0, pickup_at=base + timedelta(hours=5))
    asap = await _order(make_order, session, price="1000", km=10.0, pickup_at=None)

    assert await _ids(session, "date", "asc") == [asap, early, late]
    assert await _ids(session, "date", "desc") == [late, early, asap]
    assert await _ids(session, "recent", "desc") == [asap, late, early]
    assert await _ids(session, "recent", "asc") == [early, late, asap]


# --- Через сайт -------------------------------------------------------------------


async def test_site_accepts_new_sorts_and_direction(client, trio):
    for sort in queries.SORT_LABELS:
        if sort == "distance":
            continue
        for direction in ("asc", "desc", ""):
            response = await client.get(f"/?sort={sort}&dir={direction}")
            assert response.status_code == 200, (sort, direction)


async def test_price_decimal_is_not_broken_by_per_km_expression(session, make_order):
    """Цена — Numeric, расстояние — Float: выражение должно работать на обеих БД."""
    await _order(make_order, session, price="8000.50", km=100.0)

    page = await queries.fetch_feed(session, sort="per_km", direction="desc", page=1, page_size=5)

    assert page.items[0].client_price == Decimal("8000.50")


async def test_site_has_no_sorting_and_ignores_sort_params(client, trio):
    plain = (await client.get("/")).text
    tweaked = (await client.get("/?sort=trip_km&dir=asc")).text

    assert "sortSelect" not in plain and 'id="dirSelect"' not in plain
    ids = lambda html: re.findall(r'/orders/(\d+)/contact', html)  # noqa: E731
    assert ids(plain) == ids(tweaked) and len(ids(plain)) == 3  # порядок один и тот же: свежие сверху
