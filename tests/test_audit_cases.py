"""Аудит «нереальных заказов»: регион из текста заявки, опечатки, сбои графа дорог,
километраж из текста диспетчера. Все примеры — заказы с прода."""

import pytest
from sqlalchemy import select

from app import geo, routing
from app.db.base import SessionLocal
from app.geo import Candidate
from app.models import Order
from app.services import geo_health, routes
from app.services.geocode import geocode_pending


def _fake_nominatim(monkeypatch, answers):
    calls = []

    async def _search(query):
        calls.append(query)
        return answers.get(query, [])

    monkeypatch.setattr(geo, "_nominatim_search", _search)
    return calls


async def _geocode(session, make_order, monkeypatch, answers, *, from_city, to_city, raw_text, **fields):
    _fake_nominatim(monkeypatch, answers)
    order = await make_order(from_city=from_city, to_city=to_city, raw_text=raw_text)
    for name, value in fields.items():
        setattr(order, name, value)
    await session.commit()
    await geocode_pending()
    await session.refresh(order)
    return order


# --- Регион из текста заявки ----------------------------------------------------------


@pytest.mark.parametrize(
    ("city", "text", "expected"),
    [
        ("Село Вершины", "Завтра 6:00 | Село Вершины запорожская обл  | Ростов 1 чел 14000", "Запорожская область"),
        ("Красногвардейский", "Адлер -  Красногвардейский (Крым) 1 чел,  - 18000", "Республика Крым"),
        ("Степановка", "Степановка (Курская обл. Рыльский р-н) | Курск | 4000", "Курская область"),
        ("Село Левинка", "Кромы ( Орловская обл.) | Село Левинка ( брянсская обл.) | 9000", "Брянская область"),
        ("Каменка", "Каменка ( выборгский район , возле Питера ) | Псков", "Выборгский район"),
        ("Москва", "Москва - Тула 5000", None),
        ("Брянка ЛНР", "Брянка ЛНР - Краснодар (Краснодарский край)", None),  # свой регион уже есть
    ],
)
def test_region_from_text(city, text, expected):
    assert geo.region_from_text(text, city) == expected


def test_region_names_are_cleaned_for_nominatim():
    assert geo.normalize_place("Вершины (запорожская обл.)") == ("Вершины", "Запорожская область")
    assert geo.normalize_place("Орджоникидзе (Крым)") == ("Орджоникидзе", "Республика Крым")
    assert geo.normalize_place("Кромы ( брянсская обл.)") == ("Кромы", "Брянская область")
    assert geo.normalize_place("Корноухово, Татарстан") == ("Корноухово", "Татарстан")  # без аббревиатур — как есть


def test_prefixes_from_address_style_names_are_dropped():
    assert geo.normalize_place("сельское поселение Сабуровщино") == ("Сабуровщино", None)
    assert geo.normalize_place("Будденовск") == ("Буденновск", None)  # опечатка диспетчера


def test_airports_are_places():
    assert "aeroway" in geo._PLACE_KINDS


async def test_region_in_text_picks_the_right_homonym(session, make_order, monkeypatch):
    """Степановка: без региона — Сумская область, а в тексте — Курская."""
    sumy = Candidate(50.94, 34.64, "Степановка, Степанівська громада, Сумская область", "village")
    kursk = Candidate(51.58, 34.88, "Степановка, Рыльский район, Курская область", "village")
    order = await _geocode(
        session, make_order, monkeypatch,
        {"Степановка": [sumy], "Степановка, Курская область": [kursk]},
        from_city="Степановка", to_city="Курск",
        raw_text="Сейчас | Степановка (Курская обл. Рыльский р-н) | Курск | 4000",
    )

    assert (order.from_lat, order.from_lon) == kursk.coords


async def test_gender_variant_is_tried_in_the_named_region(session, make_order, monkeypatch):
    wrong = Candidate(56.5, 40.5, "Красногвардейский, Суздальский район, Владимирская область", "village")
    crimea = Candidate(45.49, 34.29, "Красногвардейское, Республика Крым", "town")
    order = await _geocode(
        session, make_order, monkeypatch,
        {"Красногвардейский": [wrong], "Красногвардейское, Республика Крым": [crimea]},
        from_city="Адлер", to_city="Красногвардейский",
        raw_text="04.10 Адлер - Красногвардейский (Крым) 1 чел - 18000",
    )

    assert (order.to_lat, order.to_lon) == crimea.coords


async def test_homonym_with_a_named_region_but_no_match_gets_no_coordinates(session, make_order, monkeypatch):
    """Вершины (Запорожская обл.): в Nominatim есть только самарские. Лучше без
    координат (попадёт в отчёт админки), чем 1100 км не туда."""
    samara = Candidate(53.97, 50.25, "Вершины, Елховский район, Самарская область", "village")
    order = await _geocode(
        session, make_order, monkeypatch,
        {"Вершины": [samara]},
        from_city="Село Вершины", to_city="Ростов-на-Дону",
        raw_text="Завтра 6:00 | Село Вершины запорожская обл | Ростов 1 чел 14000",
    )

    assert order.from_lat is None
    assert order.to_lat is not None  # Ростов — из справочника


async def test_found_place_in_the_named_region_is_kept(session, make_order, monkeypatch):
    kursk = Candidate(51.58, 34.88, "Степановка, Рыльский район, Курская область", "village")
    order = await _geocode(
        session, make_order, monkeypatch,
        {"Степановка": [kursk], "Степановка, Курская область": []},
        from_city="Степановка", to_city="Курск",
        raw_text="Степановка (Курская обл.) | Курск | 4000",
    )

    assert (order.from_lat, order.from_lon) == kursk.coords


async def test_district_from_address_narrows_the_search(session, make_order, monkeypatch):
    penza = Candidate(53.19, 44.05, "Каменка, Каменский район, Пензенская область", "town")
    vyborg = Candidate(60.44, 29.08, "Каменка, Выборгский район, Ленинградская область", "town")
    order = await _geocode(
        session, make_order, monkeypatch,
        {"Каменка": [penza], "Каменка, Выборгский район": [vyborg]},
        from_city="Каменка", to_city="Псков", raw_text="Каменка | Псков | 11500",
        from_address="Выборгский район, возле Питера",
    )

    assert (order.from_lat, order.from_lon) == vyborg.coords


async def test_seeded_cities_are_never_overridden_by_a_region_in_the_text(session, make_order, monkeypatch):
    calls = _fake_nominatim(monkeypatch, {})
    order = await make_order(from_city="Краснодар", to_city="Сочи", raw_text="Краснодар (Крым) - Сочи 5000")
    await geocode_pending()
    await session.refresh(order)

    assert calls == []


# --- Сбой графа дорог ------------------------------------------------------------------


def _ok(km):
    return {"code": "Ok", "routes": [{"distance": km * 1000}]}


def test_broken_road_graph_on_a_short_trip_falls_back_to_the_straight_line():
    """Алахадзы → Гагра: 9 км по прямой, OSRM вёл через полмира (3554 км)."""
    alahadzy, gagra = (43.22, 40.29), (43.30, 40.26)

    assert routing.parse_distance_km(_ok(3554), alahadzy, gagra) == pytest.approx(12.0, abs=2)


def test_broken_road_graph_on_a_long_trip_gives_no_distance():
    assert routing.parse_distance_km(_ok(5000), (55.0, 37.0), (57.0, 37.0)) is None


def test_ordinary_detours_are_accepted():
    assert routing.parse_distance_km(_ok(250), (55.0, 37.0), (57.0, 37.0)) == 250.0


# --- Километраж, который написал диспетчер -------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Расстояние: 70 км.", 70.0),
        ("🗺 382 км • 5 ч 39 мин", 382.0),
        ("срочно время | Троицк | Смоленск | 395 км 11.000", 395.0),
        ("Расстояние: 14,5 км", 14.5),
        ("до 60 км/ч", None),
        ("+20 км за город, всего 300 км", None),  # два километража — неясно, какой
        ("ЭКО 4 км", None),  # слишком мало
        ("Москва - Тула 5000", None),
        (None, None),
    ],
)
def test_parse_stated_km(text, expected):
    assert routing.parse_stated_km(text) == expected


async def _route(session, make_order, monkeypatch, osrm_km, raw_text, **kw):
    async def fake(origin, destination, via=None):
        return osrm_km

    monkeypatch.setattr(routing, "road_distance_km", fake)
    order = await make_order(raw_text=raw_text, from_coords=(55.0, 37.0), to_coords=(57.0, 37.0), **kw)
    await routes.route_pending()
    async with SessionLocal() as fresh:
        return (await fresh.execute(select(Order).where(Order.id == order.id))).scalar_one()


async def test_stated_distance_wins_when_the_calculation_is_far_off(session, make_order, monkeypatch):
    """Звенигород → Шереметьево: нашли липецкое Шереметьево (464 км), в заявке 70 км."""
    order = await _route(session, make_order, monkeypatch, 463.6, "Откуда: Звенигород | Расстояние: 70 км. | 5000")

    assert order.distance_km == 70.0


async def test_stated_distance_is_ignored_when_it_agrees_with_the_calculation(session, make_order, monkeypatch):
    order = await _route(session, make_order, monkeypatch, 250.0, "Расстояние: 240 км. | 9000")

    assert order.distance_km == 250.0  # в пределах допуска — оставляем расчёт


async def test_stated_distance_fills_in_when_there_is_no_route(session, make_order, monkeypatch):
    order = await _route(session, make_order, monkeypatch, None, "395 км 11.000")

    assert order.distance_km == 395.0


# --- Отчёт -----------------------------------------------------------------------------------


def test_trip_inside_one_city_is_not_a_problem():
    order = Order(
        from_city="Краснодар", to_city="Краснодар", from_city_key="краснодар", to_city_key="краснодар",
        from_lat=45.0, from_lon=39.0, to_lat=45.0, to_lon=39.0, geo_checked_at=geo.now_utc_naive(),
    )

    assert geo_health.classify(order) is None


# --- Неверное число не должно расходиться по заказам -----------------------------------------


async def test_poisoned_distance_of_a_donor_is_not_copied(session, make_order, monkeypatch):
    """Алахадзы → Гагра: донор хранит 3554 км при прямой в 9 км."""
    from app.timeutil import now_utc_naive

    async def fake(origin, destination, via=None):
        return 14.0

    monkeypatch.setattr(routing, "road_distance_km", fake)
    a, b = (43.22, 40.29), (43.30, 40.26)
    donor = await make_order(from_coords=a, to_coords=b)
    async with SessionLocal() as fresh:
        row = await fresh.get(Order, donor.id)
        row.distance_km = 3554.0
        row.route_checked_at = now_utc_naive()
        await fresh.commit()
    order = await make_order(from_coords=a, to_coords=b)

    await routes.route_pending()

    async with SessionLocal() as fresh:
        assert (await fresh.get(Order, order.id)).distance_km == 14.0


async def test_donor_with_an_old_geocoder_version_is_not_used(session, make_order, monkeypatch):
    from app.timeutil import now_utc_naive

    async def fake(origin, destination, via=None):
        return 200.0

    monkeypatch.setattr(routing, "road_distance_km", fake)
    a, b = (55.0, 37.0), (57.0, 37.0)
    donor = await make_order(from_coords=a, to_coords=b)
    async with SessionLocal() as fresh:
        row = await fresh.get(Order, donor.id)
        row.distance_km = 230.0  # правдоподобно, но посчитано старой логикой
        row.route_checked_at = now_utc_naive()
        row.geo_version = geo.GEO_VERSION - 1
        await fresh.commit()
    order = await make_order(from_coords=a, to_coords=b)

    await routes.route_pending()

    async with SessionLocal() as fresh:
        assert (await fresh.get(Order, order.id)).distance_km == 200.0


async def test_heal_resets_only_insane_distances(session, make_order):
    from app.timeutil import now_utc_naive

    bad = await make_order(from_coords=(43.22, 40.29), to_coords=(43.30, 40.26))
    good = await make_order(from_coords=(55.0, 37.0), to_coords=(57.0, 37.0))
    stated = await make_order(
        from_coords=(55.0, 37.0), to_coords=(57.0, 37.0), raw_text="Расстояние: 395 км"
    )
    async with SessionLocal() as fresh:
        for order, km in ((bad, 3554.0), (good, 250.0), (stated, 395.0)):
            row = await fresh.get(Order, order.id)
            row.distance_km = km
            row.route_checked_at = now_utc_naive()
        await fresh.commit()

    assert await routes.heal_stale_routes() == 1

    async with SessionLocal() as fresh:
        assert (await fresh.get(Order, bad.id)).route_checked_at is None
        assert (await fresh.get(Order, good.id)).distance_km == 250.0
        assert (await fresh.get(Order, stated.id)).distance_km == 395.0  # из текста диспетчера


# --- Адрес-населённый пункт не должен уводить заказ за тысячи километров --------------------------


async def test_address_matching_a_far_namesake_does_not_move_the_order(session, make_order, monkeypatch):
    """Алахадзы → Гагра, адрес «тихая гавань» (пляж): одноимённый посёлок есть на Кольском."""
    alahadzy = Candidate(43.22, 40.29, "Алахадзы, Гагрский район, Абхазия", "village")
    gagra = Candidate(43.30, 40.26, "Гагра, Гагрский район, Абхазия", "town")
    kola = Candidate(68.26, 32.48, "Тихая Гавань, Печенгский округ, Мурманская область", "locality")
    order = await _geocode(
        session, make_order, monkeypatch,
        {"Алахадзы": [alahadzy], "Гагра": [gagra], "тихая гавань": [kola], "Тихая гавань": [kola]},
        from_city="Алахадзы", to_city="Гагра", raw_text="сейчас Алахадзы тихая гавань - Гагра центр 900 ЭКО",
        from_address="тихая гавань", to_address="центр",
    )

    assert (order.from_lat, order.from_lon) == alahadzy.coords


async def test_address_is_not_trusted_without_the_other_end_of_the_route(session, make_order, monkeypatch):
    """Другого конца нет в справочнике и не нашёлся — проверить адрес нечем."""
    wrong = Candidate(50.43, 30.12, "Мрия, Киевская область", "village")
    elsewhere = Candidate(44.40, 33.94, "Оползневое, Ялтинский совет, Крым", "village")
    order = await _geocode(
        session, make_order, monkeypatch,
        {"Мрия": [wrong], "Оползневое": [elsewhere]},
        from_city="Мрия", to_city="Нигдеград", raw_text="Мрия - Нигдеград 3000", from_address="Оползневое",
    )

    assert (order.from_lat, order.from_lon) == wrong.coords


# --- Село, которого нет в OSM, но рядом названо известное место ---------------------------------------

VOLNOVAKHA = Candidate(47.60, 37.49, "Волноваха, Волновахская городская община, Донецкая область", "town")


async def test_village_not_in_osm_is_placed_next_to_the_named_neighbour(session, make_order, monkeypatch):
    """«Хлебодаровка (Волноваха)»: заказ 2030 остался без расстояния."""
    order = await _geocode(
        session, make_order, monkeypatch,
        {"Волноваха": [VOLNOVAKHA]},
        from_city="Хлебодаровка (Волноваха)", to_city="Астрахань",
        raw_text="04.10 21.00 Хлебодаровка (Волноваха)   Астрахань 28 000",
    )

    assert (order.from_lat, order.from_lon) == VOLNOVAKHA.coords


async def test_a_namesake_near_the_neighbour_beats_the_neighbour_itself(session, make_order, monkeypatch):
    village = Candidate(47.45, 37.30, "Хлебодаровка, Волновахский район, Донецкая область", "village")
    order = await _geocode(
        session, make_order, monkeypatch,
        {"Волноваха": [VOLNOVAKHA], "Хлебодаровка": [village]},
        from_city="Хлебодаровка (Волноваха)", to_city="Астрахань",
        raw_text="Хлебодаровка (Волноваха) - Астрахань 28000",
    )

    assert (order.from_lat, order.from_lon) == village.coords


async def test_a_far_namesake_is_not_taken_instead_of_the_neighbour(session, make_order, monkeypatch):
    far = Candidate(55.0, 60.0, "Хлебодаровка, Челябинская область", "village")
    order = await _geocode(
        session, make_order, monkeypatch,
        {"Волноваха": [VOLNOVAKHA], "Хлебодаровка": [far]},
        from_city="Хлебодаровка (Волноваха)", to_city="Астрахань",
        raw_text="Хлебодаровка (Волноваха) - Астрахань 28000",
    )

    assert (order.from_lat, order.from_lon) == VOLNOVAKHA.coords


async def test_region_in_brackets_is_never_used_as_a_neighbour(session, make_order, monkeypatch):
    """Регион — не населённый пункт: приблизительной точки по нему не ставим."""
    calls = _fake_nominatim(monkeypatch, {})
    order = await make_order(from_city="СНТ Перемяки (Ленинградская обл.)", to_city="Москва")
    await geocode_pending()
    await session.refresh(order)

    assert order.from_lat is None
    assert "Ленинградская область" not in " ".join(c for c in calls if c.startswith("Ленинградская"))


async def test_not_found_checked_before_the_logic_changed_is_searched_again(session, monkeypatch):
    from datetime import timedelta

    from app.models import GeoPlace
    from app.timeutil import now_utc_naive

    found = Candidate(44.0, 34.0, "Нашлось, Республика Крым", "village")
    calls = _fake_nominatim(monkeypatch, {"Забытое": [found]})
    session.add(GeoPlace(key=geo.place_key("Забытое", None), candidates=[], status="not_found",
                         source="nominatim", checked_at=geo.GEO_LOGIC_DATE - timedelta(days=1)))
    await session.commit()

    assert (await geo.resolve(session, "Забытое"))[0].coords == found.coords
    assert calls == ["Забытое"]
    # А свежее «не найдено» по-прежнему не переспрашиваем.
    session.add(GeoPlace(key=geo.place_key("Пропавшее", None), candidates=[], status="not_found",
                         source="nominatim", checked_at=now_utc_naive()))
    await session.commit()
    assert await geo.resolve(session, "Пропавшее") == []
    assert calls == ["Забытое"]


async def test_orders_without_coordinates_are_rechecked_after_the_logic_date(session, make_order, monkeypatch):
    from datetime import timedelta

    _fake_nominatim(monkeypatch, {"Волноваха": [VOLNOVAKHA]})
    order = await make_order(from_city="Хлебодаровка (Волноваха)", to_city="Астрахань")
    async with SessionLocal() as fresh:
        row = await fresh.get(Order, order.id)
        row.from_lat = row.from_lon = row.to_lat = row.to_lon = None
        row.geo_checked_at = geo.GEO_LOGIC_DATE - timedelta(hours=1)
        row.geo_version = geo.GEO_VERSION
        await fresh.commit()

    assert await geocode_pending() == 1
    await session.refresh(order)
    assert order.from_lat is not None
    assert await geocode_pending() == 0
