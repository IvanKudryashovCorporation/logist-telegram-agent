"""Радиус «откуда/куда»: нормализация названий, геокодер, SQL-фильтр и его эталон.

В сеть тесты не ходят: ``geo._nominatim_search`` подменяется в каждом тесте,
где геокодеру вообще разрешено работать.
"""

from datetime import timedelta

import pytest
from sqlalchemy import select

from app import geo
from app.geo import Candidate, GeocoderUnavailable
from app.models import GeoPlace, Order
from app.search import refresh_derived
from app.services.geocode import geocode_pending
from app.timeutil import now_msk_naive, now_utc_naive
from app.web import queries
from app.web.filters import Filters

KRASNODAR = (45.0355, 38.9753)
#: ~14 км и ~58 км от Краснодара; Сочи — ~170 км.
NEAR_VILLAGE = (45.15, 39.05)
FAR_VILLAGE = (45.50, 39.30)
SOCHI = (43.5855, 39.7231)


@pytest.fixture(autouse=True)
def no_real_network(monkeypatch):
    """По умолчанию любой выход в Nominatim — ошибка теста."""

    async def _forbidden(query):
        raise AssertionError(f"тест полез в сеть: {query!r}")

    monkeypatch.setattr(geo, "_nominatim_search", _forbidden)


# --- Нормализация названий ----------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("ст. Голубицкая", ("Голубицкая", None)),
        ("станица Голубицкая", ("Голубицкая", None)),
        ("село Титовка", ("Титовка", None)),
        ("с. Красное", ("Красное", None)),
        ("кпп Должанский", ("Должанский", None)),
        ("Брянка ЛНР", ("Брянка", "Луганская область")),
        ("Кумшатское ДНР", ("Кумшатское", "Донецкая область")),
        ("Торез (ДНР)", ("Торез", "Донецкая область")),
        ("Корноухово, Татарстан", ("Корноухово", "Татарстан")),
        ("с. Соленое озеро (Джанкой)", ("Соленое озеро", "Джанкой")),
        ("Симферополь, аэропорт", ("Симферополь", None)),
        ("Симферополь (аэропорт)", ("Симферополь", None)),
        ("Мин Воды", ("Минеральные Воды", None)),
        ("г. Краснодар", ("Краснодар", None)),
        ("  ", ("", None)),
        (None, ("", None)),
    ],
)
def test_normalize_place(raw, expected):
    assert geo.normalize_place(raw) == expected


def test_normalize_keeps_villages_that_merely_start_like_a_city():
    # «Краснодарский» — посёлок, а не Краснодар: префиксного раскрытия алиасов нет.
    assert geo.normalize_place("Краснодарский") == ("Краснодарский", None)


def test_place_key_depends_on_region_hint():
    assert geo.place_key("Орджоникидзе", None) != geo.place_key("Орджоникидзе", "Крым")


@pytest.mark.parametrize(
    ("value", "expected"),
    [("25", 25), ("200", 200), ("0", 0), ("", 0), ("abc", 0), ("30", 0), ("-50", 0), ("1000000", 0)],
)
def test_parse_radius_whitelist(value, expected):
    assert geo.parse_radius(value) == expected


# --- Выбор кандидата и расстояния -------------------------------------------


def test_pick_prefers_candidate_near_other_end():
    crimea = Candidate(44.96, 35.36, "Орджоникидзе, Крым")
    kuban = Candidate(44.97, 37.77, "Орджоникидзе, Краснодарский край")
    feodosia = (45.03, 35.38)

    assert geo.pick([kuban, crimea], near=feodosia) == crimea
    assert geo.pick([kuban, crimea]) == kuban  # без подсказки — самый значимый
    assert geo.pick([]) is None


def test_flat_distance_is_close_to_haversine():
    flat = geo.flat_distance_km(KRASNODAR, FAR_VILLAGE)
    exact = geo.haversine_km(KRASNODAR, FAR_VILLAGE)
    assert abs(flat - exact) / exact < 0.01


# --- Фильтр: SQL и эталон -----------------------------------------------------


async def _seed_orders(make_order) -> dict[str, int]:
    when = now_msk_naive() + timedelta(days=2)
    ids = {}
    ids["city"] = (await make_order(
        from_city="Краснодар", from_coords=KRASNODAR, pickup_at=when)).id
    ids["city_no_coords"] = (await make_order(from_city="Краснодар", pickup_at=when)).id
    ids["near"] = (await make_order(
        from_city="станица Динская", from_coords=NEAR_VILLAGE, pickup_at=when)).id
    ids["far"] = (await make_order(
        from_city="хутор Дальний", from_coords=FAR_VILLAGE, pickup_at=when)).id
    ids["sochi"] = (await make_order(
        from_city="Сочи", from_coords=SOCHI, pickup_at=when)).id
    ids["village_no_coords"] = (await make_order(
        from_city="посёлок Безымянный", pickup_at=when)).id
    return ids


def _filters(radius: str, **extra) -> Filters:
    filters = Filters(from_city="Краснодар", from_radius=radius, **extra)
    if filters.from_radius:
        filters.from_centers = {"Краснодар": KRASNODAR}
    return filters


async def _feed_ids(session, filters: Filters) -> set[int]:
    page = await queries.fetch_feed(session, filters=filters, page_size=50)
    return {order.id for order in page.items}


@pytest.mark.parametrize(
    ("radius", "expected"),
    [
        ("0", {"city", "city_no_coords"}),
        ("25", {"city", "city_no_coords", "near"}),
        ("50", {"city", "city_no_coords", "near"}),
        ("100", {"city", "city_no_coords", "near", "far"}),
        # Сочи в ~170 км от Краснодара: в 100 км не входит, в 200 — входит.
        ("200", {"city", "city_no_coords", "near", "far", "sochi"}),
    ],
)
async def test_radius_selects_orders_inside_circle(session, make_order, radius, expected):
    ids = await _seed_orders(make_order)

    found = await _feed_ids(session, _filters(radius))

    assert found == {ids[name] for name in expected}


async def test_radius_never_drops_name_matches_without_coordinates(session, make_order):
    ids = await _seed_orders(make_order)

    with_radius = await _feed_ids(session, _filters("100"))

    assert ids["city_no_coords"] in with_radius


@pytest.mark.parametrize("radius", ["0", "25", "50", "100", "200"])
async def test_sql_and_python_reference_agree_with_radius(session, make_order, radius):
    await _seed_orders(make_order)
    filters = _filters(radius)

    sql_ids = await _feed_ids(session, filters)
    orders = list((await session.execute(select(Order))).scalars().all())
    python_ids = {order.id for order in orders if filters.matches(order)}

    assert sql_ids == python_ids


async def test_radius_without_known_center_falls_back_to_name_only(session, make_order):
    ids = await _seed_orders(make_order)
    filters = Filters(from_city="Краснодар", from_radius="100")  # центров нет

    assert await _feed_ids(session, filters) == {ids["city"], ids["city_no_coords"]}


async def test_radius_applies_to_destination_too(session, make_order):
    when = now_msk_naive() + timedelta(days=2)
    near = await make_order(to_city="посёлок Рядом", to_coords=NEAR_VILLAGE, pickup_at=when)
    await make_order(to_city="Сочи", to_coords=SOCHI, pickup_at=when)
    filters = Filters(to_city="Краснодар", to_radius="25")
    filters.to_centers = {"Краснодар": KRASNODAR}

    assert await _feed_ids(session, filters) == {near.id}


async def test_radius_notes_explain_why_village_is_in_results(session, make_order):
    ids = await _seed_orders(make_order)
    filters = _filters("50")
    orders = {
        order.id: order
        for order in (await session.execute(select(Order))).scalars().all()
    }

    assert filters.radius_notes(orders[ids["near"]])["from"].startswith("~14 км от «Краснодар»")
    assert filters.radius_notes(orders[ids["city"]])["from"] is None
    assert filters.radius_notes(orders[ids["sochi"]])["from"] is None


def test_radius_is_not_counted_as_active_filter():
    assert Filters(from_radius="50").active_count == 0
    assert Filters(from_city="Краснодар", from_radius="50").active_count == 1
    assert Filters(from_radius="50").as_dict()["from_radius"] == "50"


# --- Координаты сбрасываются при смене города ---------------------------------


async def test_refresh_derived_clears_coordinates_when_city_changes(make_order):
    order = await make_order(from_city="Краснодар", from_coords=KRASNODAR)
    assert order.from_lat is not None

    refresh_derived(order)  # город не менялся — координаты остаются
    assert order.from_lat == KRASNODAR[0]
    assert order.geo_checked_at is not None

    order.from_city = "Сочи"
    refresh_derived(order)
    assert order.from_lat is None and order.from_lon is None
    assert order.geo_checked_at is None


# --- Геокодер и фоновый воркер -----------------------------------------------


def _fake_nominatim(monkeypatch, answers: dict[str, list[Candidate]]) -> list[str]:
    calls: list[str] = []

    async def _search(query):
        calls.append(query)
        return answers.get(query, [])

    monkeypatch.setattr(geo, "_nominatim_search", _search)
    return calls


async def test_worker_geocodes_orders_and_caches_lookups(session, make_order, monkeypatch):
    calls = _fake_nominatim(
        monkeypatch, {"Голубицкая": [Candidate(45.2, 36.9, "Голубицкая", "village")]}
    )
    first = await make_order(from_city="ст. Голубицкая", to_city="Краснодар")
    second = await make_order(from_city="ст. Голубицкая", to_city="Краснодар")

    processed = await geocode_pending()

    assert processed == 2
    assert calls == ["Голубицкая"]  # второй заказ — из кэша, Краснодар — из справочника
    await session.refresh(first)
    await session.refresh(second)
    for order in (first, second):
        assert (order.from_lat, order.from_lon) == (45.2, 36.9)
        assert (order.to_lat, order.to_lon) == KRASNODAR
        assert order.geo_checked_at is not None
    assert await geocode_pending() == 0  # всё обработано


async def test_worker_marks_unknown_places_checked_without_coordinates(
    session, make_order, monkeypatch
):
    _fake_nominatim(monkeypatch, {})
    order = await make_order(from_city="Несуществующее", to_city="Краснодар")

    await geocode_pending()

    await session.refresh(order)
    assert order.from_lat is None
    assert order.geo_checked_at is not None
    cached = (await session.execute(select(GeoPlace))).scalars().all()
    assert [(place.key, place.status) for place in cached] == [("несуществующее|", "not_found")]


async def test_not_found_is_retried_only_after_a_week(session, make_order, monkeypatch):
    calls = _fake_nominatim(monkeypatch, {})
    await make_order(from_city="Несуществующее", to_city="Краснодар")
    await geocode_pending()
    assert len(calls) == 1

    # Повторный разбор в тот же день сеть не трогает.
    await session.execute(Order.__table__.update().values(geo_checked_at=None))
    await session.commit()
    await geocode_pending()
    assert len(calls) == 1

    place = (await session.execute(select(GeoPlace))).scalar_one()
    place.checked_at = now_utc_naive() - timedelta(days=8)
    await session.commit()
    await session.execute(Order.__table__.update().values(geo_checked_at=None))
    await session.commit()
    await geocode_pending()
    assert len(calls) == 2


async def test_worker_picks_homonym_closest_to_other_end(session, make_order, monkeypatch):
    crimea = Candidate(44.96, 35.36, "Орджоникидзе, Крым", "town")
    kuban = Candidate(44.97, 37.77, "Орджоникидзе, Кубань", "hamlet")
    _fake_nominatim(monkeypatch, {"Орджоникидзе": [kuban, crimea]})
    order = await make_order(from_city="Орджоникидзе", to_city="Феодосия")

    await geocode_pending()

    await session.refresh(order)
    assert (order.from_lat, order.from_lon) == (crimea.lat, crimea.lon)


async def test_worker_leaves_order_unchecked_when_geocoder_is_down(
    session, make_order, monkeypatch
):
    async def _down(query):
        raise GeocoderUnavailable("нет сети")

    monkeypatch.setattr(geo, "_nominatim_search", _down)
    order = await make_order(from_city="Голубицкая", to_city="Краснодар")

    with pytest.raises(GeocoderUnavailable):
        await geocode_pending()

    await session.refresh(order)
    assert order.geo_checked_at is None  # попробуем на следующем проходе
    assert (await session.execute(select(GeoPlace))).scalars().all() == []


async def test_geocoding_does_not_touch_updated_at(session, make_order, monkeypatch):
    _fake_nominatim(monkeypatch, {})
    order = await make_order(from_city="Краснодар", to_city="Сочи")
    before = (await session.execute(select(Order.updated_at).where(Order.id == order.id))).scalar_one()

    await geocode_pending()

    after = (await session.execute(select(Order.updated_at).where(Order.id == order.id))).scalar_one()
    assert after == before


async def test_centers_for_reads_cache_only(session, monkeypatch):
    session.add(
        GeoPlace(
            key="голубицкая|", candidates=[{"lat": 45.2, "lon": 36.9, "name": "x", "kind": "village"}],
            status="ok", source="nominatim", checked_at=now_utc_naive(),
        )
    )
    await session.commit()

    centers, missing = await geo.centers_for(session, "Краснодар, станица Голубицкая, Неизвестный")

    assert centers == {"Краснодар": KRASNODAR, "станица Голубицкая": (45.2, 36.9)}
    assert missing == ["Неизвестный"]  # без обращения к сети — autouse-заглушка бы упала


async def test_feed_route_reports_unresolved_radius_center(client, make_order):
    await make_order(from_city="Краснодар", pickup_at=now_msk_naive() + timedelta(days=2))

    response = await client.get("/", params={"from_city": "Неизвестный", "from_radius": "50"})

    assert response.status_code == 200
    assert "радиус не применён" in response.text
    assert "Неизвестный" in response.text


async def test_feed_route_applies_radius_and_keeps_it_in_form(client, make_order):
    when = now_msk_naive() + timedelta(days=2)
    near = await make_order(from_city="посёлок Рядом", from_coords=NEAR_VILLAGE, pickup_at=when)
    far = await make_order(from_city="Сочи", from_coords=SOCHI, pickup_at=when)

    response = await client.get("/", params={"from_city": "Краснодар", "from_radius": "25"})

    assert response.status_code == 200
    assert f'href="/orders/{near.id}' in response.text
    assert f'href="/orders/{far.id}' not in response.text
    assert "~14 км от «Краснодар»" in response.text
    assert 'name="from_radius" value="25"' in response.text


# --- Адрес поправляет неверно найденную деревню -----------------------------------

KIEV_MRIYA = Candidate(50.437, 30.128, "Мрия, Бучанский район, Киевская область, Украина", "village")
CRIMEA_OPOLZNEVOE = Candidate(
    44.410, 33.940, "Оползневое, Симеизский поселковый совет, Ялтинский городской совет", "village"
)


async def _geocode_one(session, make_order, monkeypatch, answers, *, address, city="Мрия"):
    calls = _fake_nominatim(monkeypatch, answers)
    order = await make_order(from_city=city, to_city="Севастополь")
    order.from_address = address
    await session.commit()
    await geocode_pending()
    await session.refresh(order)
    return order, calls


async def test_address_that_is_a_place_overrides_a_far_wrong_village(session, make_order, monkeypatch):
    """«Мрия» нашлась только под Киевом, а адрес «Оползневое» — в Крыму."""
    order, _ = await _geocode_one(
        session, make_order, monkeypatch,
        {"Мрия": [KIEV_MRIYA], "Оползневое": [CRIMEA_OPOLZNEVOE]},
        address="Оползневое",
    )

    assert (order.from_lat, order.from_lon) == CRIMEA_OPOLZNEVOE.coords


async def test_address_close_to_the_found_village_changes_nothing(session, make_order, monkeypatch):
    near = Candidate(50.50, 30.20, "Оползневое, Киевская область", "village")  # ~10 км от Мрии

    order, _ = await _geocode_one(
        session, make_order, monkeypatch, {"Мрия": [KIEV_MRIYA], "Оползневое": [near]},
        address="Оползневое",
    )

    assert (order.from_lat, order.from_lon) == KIEV_MRIYA.coords


async def test_street_addresses_are_never_looked_up(session, make_order, monkeypatch):
    order, calls = await _geocode_one(
        session, make_order, monkeypatch, {"Мрия": [KIEV_MRIYA]}, address="ул. Ленина 5",
    )

    assert calls == ["Мрия"]
    assert (order.from_lat, order.from_lon) == KIEV_MRIYA.coords


async def test_ambiguous_address_is_not_trusted(session, make_order, monkeypatch):
    """Два «Оползневых» в разных местах — какому верить, неизвестно."""
    other = Candidate(55.0, 60.0, "Оползневое, Челябинская область", "village")

    order, _ = await _geocode_one(
        session, make_order, monkeypatch,
        {"Мрия": [KIEV_MRIYA], "Оползневое": [CRIMEA_OPOLZNEVOE, other]},
        address="Оползневое",
    )

    assert (order.from_lat, order.from_lon) == KIEV_MRIYA.coords


async def test_big_cities_are_not_overridden_by_address(session, make_order, monkeypatch):
    """Краснодар из справочника — адрес «Центральный» его не двигает."""
    district = Candidate(55.0, 60.0, "Центральный, Челябинская область", "village")
    calls = _fake_nominatim(monkeypatch, {"Центральный": [district]})
    order = await make_order(from_city="Краснодар", to_city="Сочи")
    order.from_address = "Центральный"
    await session.commit()

    await geocode_pending()
    await session.refresh(order)

    assert (order.from_lat, order.from_lon) == KRASNODAR
    assert calls == []
