"""Места, которые геокодер раньше терял, хотя Nominatim их знает (заказы с прода).

* СНТ и «дачи» — отдельный тип места в OpenStreetMap (``allotments``), его отбрасывал фильтр;
* «Пенкино (Зарайский округ)» — запрос с районом ничего не даёт, а без него находится
  десяток одноимённых деревень: нужна та, в адресе которой есть названный район.
"""

from app import geo
from app.geo import Candidate


def _fake_nominatim(monkeypatch, answers):
    calls = []

    async def _search(query):
        calls.append(query)
        return answers.get(query, [])

    monkeypatch.setattr(geo, "_nominatim_search", _search)
    return calls


def _candidate(name, lat=55.0, lon=38.0, kind="hamlet"):
    return Candidate(lat=lat, lon=lon, name=name, kind=kind)


# --- Район или область в адресе ----------------------------------------------------------------


def test_hint_stems_skip_generic_words():
    assert geo._hint_stems("Зарайский округ") == ["зарай"]
    assert geo._hint_stems("Запорожская область") == ["запор"]
    assert geo._hint_stems("Амвросиевский МО") == ["амвро"]
    assert geo._hint_stems("район Каховки") == ["кахов"]
    assert geo._hint_stems("область") == []  # одни общие слова — искать нечем


async def test_village_is_found_by_the_district_in_its_address(session, monkeypatch):
    calls = _fake_nominatim(
        monkeypatch,
        {
            "Пенкино": [
                _candidate("Пенкино, Никольск, Вилегодский муниципальный округ, Архангельская область", 61.5, 45.0),
                _candidate("Пенкино, муниципальный округ Зарайск, Московская область, Россия", 54.7, 38.9),
            ]
        },
    )

    found = await geo.resolve(session, "Пенкино (Зарайский округ)")

    assert [c.name.split(",")[0] for c in found] == ["Пенкино"]
    assert "Зарайск" in found[0].name  # не архангельская деревня с тем же названием
    assert calls == ["Пенкино, Зарайский округ", "Пенкино"]


async def test_district_not_in_any_address_stays_not_found(session, monkeypatch):
    """Совпадения по названию мало: без названного района угадывать деревню нельзя."""
    _fake_nominatim(
        monkeypatch,
        {"Орлик": [_candidate("Орлик, Окинский муниципальный округ, Бурятия", 52.0, 100.0)]},
    )

    assert await geo.resolve(session, "село Орлик (Белгородский р-н)") == []


async def test_exact_regional_query_is_tried_first(session, monkeypatch):
    calls = _fake_nominatim(
        monkeypatch,
        {"Кромы, Орловская область": [_candidate("Кромы, Орловская область", 52.6, 35.8, "town")]},
    )

    found = await geo.resolve(session, "Кромы (Орловская обл.)")

    assert len(found) == 1
    assert calls == ["Кромы, Орловская область"]  # лишних запросов в сеть нет


# --- СНТ и дачи ---------------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, rows):
        self._rows = rows

    def raise_for_status(self):
        return None

    def json(self):
        return self._rows


def _fake_http(monkeypatch, rows):
    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, *args, **kwargs):
            return _FakeResponse(rows)

    monkeypatch.setattr(geo.httpx, "AsyncClient", _Client)
    monkeypatch.setattr(geo, "_last_request_at", 0.0)


async def test_garden_partnerships_are_accepted_as_places(monkeypatch):
    _fake_http(
        monkeypatch,
        [
            {"lat": "55.7", "lon": "38.1", "addresstype": "allotments", "type": "allotments",
             "display_name": "СНТ «Садко», Богородский городской округ, Московская область"},
            {"lat": "55.0", "lon": "37.0", "addresstype": "road", "type": "residential",
             "display_name": "улица Садко, Москва"},
        ],
    )

    found = await geo._nominatim_search("Снт Садко, Московская область")

    assert [c.kind for c in found] == ["allotments"]  # дорога по-прежнему не считается местом


def test_allotments_count_as_small_places_for_address_refinement():
    assert "allotments" in geo._SMALL_PLACE_KINDS and "allotments" in geo._PLACE_KINDS


# --- Район из адреса не перечёркивает место, найденное по региону из названия ---------------------


async def _geocode_one(session, make_order, monkeypatch, answers, **fields):
    from app.services.geocode import geocode_pending

    _fake_nominatim(monkeypatch, answers)
    order = await make_order(**fields)
    await session.commit()
    await geocode_pending()
    await session.refresh(order)
    return order


async def test_district_in_the_address_does_not_drop_a_city_found_by_its_own_region(
    session, make_order, monkeypatch
):
    """«Донецк (ДНР)», адрес «Петровский р-н»: Петровский район — район самого Донецка."""
    donetsk = Candidate(
        lat=48.0, lon=37.8, kind="city",
        name="Донецк, Ворошиловский район, Донецкая городская община, Донецкий район, Донецкая область",
    )
    order = await _geocode_one(
        session, make_order, monkeypatch, {"Донецк, Донецкая область": [donetsk]},
        from_city="Донецк (ДНР)", to_city="Москва",
    )
    order.from_address = "Петровский р-н"
    order.geo_checked_at = None
    await session.commit()
    from app.services.geocode import geocode_pending

    await geocode_pending()
    await session.refresh(order)

    assert (order.from_lat, order.from_lon) == (48.0, 37.8)


async def test_inflected_district_name_does_not_drop_the_village(session, make_order, monkeypatch):
    """«Писково (Московская обл.)», адрес «Истринский р-н…»: в названии места — «Истра», не «Истринский»."""
    pi = Candidate(lat=55.9, lon=36.8, kind="hamlet", name="Писково, муниципальный округ Истра, Московская область, Россия")
    order = await _geocode_one(
        session, make_order, monkeypatch, {"Писково, Московская область": [pi]},
        from_city="Писково (Московская обл.)", to_city="Калуга",
    )
    order.from_address = "Истринский р-н, деревня Писково, д.60"
    order.geo_checked_at = None
    await session.commit()
    from app.services.geocode import geocode_pending

    await geocode_pending()
    await session.refresh(order)

    assert (order.from_lat, order.from_lon) == (55.9, 36.8)


async def test_namesake_in_another_region_is_still_rejected_without_own_region(session, make_order, monkeypatch):
    """Защита осталась: город БЕЗ региона в названии и найденный не в регионе адреса — координат нет."""
    wrong = Candidate(lat=61.5, lon=45.0, kind="hamlet", name="Пенкино, Вилегодский муниципальный округ, Архангельская область")
    order = await _geocode_one(
        session, make_order, monkeypatch, {"Пенкино": [wrong]},
        from_city="Пенкино", to_city="Москва",
    )
    order.from_address = "Московская область, Зарайск"
    order.geo_checked_at = None
    await session.commit()
    from app.services.geocode import geocode_pending

    await geocode_pending()
    await session.refresh(order)

    assert order.from_lat is None


# --- Опечатки: нечёткий поиск ---------------------------------------------------------------------


def _photon(name, state, county, lat, lon, value="town"):
    return {
        "geometry": {"coordinates": [lon, lat]},
        "properties": {"name": name, "osm_value": value, "state": state, "county": county, "country": "Россия"},
    }


def _fake_photon(monkeypatch, features):
    calls = []

    async def _search(query):
        calls.append(query)
        return features

    monkeypatch.setattr(geo, "_photon_search", _search)
    return calls


async def test_typo_is_found_and_spelled_correctly(session, monkeypatch):
    _fake_nominatim(monkeypatch, {})
    calls = _fake_photon(monkeypatch, [_photon("Острогожск", "Воронежская область", "Острогожский район", 50.87, 39.07)])

    found = await geo.resolve(session, "Острогоржск (Воронежская обл.)")

    assert [(c.canonical, round(c.lat, 2)) for c in found] == [("Острогожск", 50.87)]
    assert calls == ["Острогоржск Воронежская область"]


async def test_typo_in_the_wrong_region_is_not_accepted(session, monkeypatch):
    _fake_nominatim(monkeypatch, {})
    _fake_photon(monkeypatch, [_photon("Острогожск", "Липецкая область", "Другой район", 52.0, 39.0)])

    assert await geo.resolve(session, "Острогоржск (Воронежская обл.)") == []


async def test_without_a_region_only_a_near_exact_spelling_is_accepted(session, monkeypatch):
    _fake_nominatim(monkeypatch, {})
    _fake_photon(
        monkeypatch,
        [
            _photon("Кондровка", "Белгородская область", "Прохоровский округ", 51.0, 36.7, "hamlet"),
            _photon("Кондаковка", "Астраханская область", "Красноярский округ", 46.0, 48.0, "hamlet"),
        ],
    )

    found = await geo.resolve(session, "Кондоровка")

    assert [c.canonical for c in found] == ["Кондровка"]  # «Кондаковка» слишком не похожа


async def test_short_names_are_never_corrected(session, monkeypatch):
    """«Орлик» и «Орёл» — разные места: у коротких названий опечатки не угадываем."""
    _fake_nominatim(monkeypatch, {})
    calls = _fake_photon(monkeypatch, [_photon("Орёл", "Орловская область", "", 52.9, 36.0, "city")])

    assert await geo.resolve(session, "Орлик") == []
    assert calls == []


async def test_correctly_spelled_name_gets_no_correction(session, monkeypatch):
    _fake_nominatim(monkeypatch, {})
    _fake_photon(monkeypatch, [_photon("Каргаполье", "Курганская область", "Каргапольский округ", 55.9, 65.9)])

    found = await geo.resolve(session, "Каргаполье (Курганская обл.)")

    assert found and found[0].canonical == ""


async def test_typo_search_failure_is_not_cached_as_not_found(session, monkeypatch):
    _fake_nominatim(monkeypatch, {})

    async def _down(query):
        raise geo.GeocoderUnavailable("photon down")

    monkeypatch.setattr(geo, "_photon_search", _down)

    assert await geo.resolve(session, "Острогоржск (Воронежская обл.)") == []
    from sqlalchemy import select

    from app.models import GeoPlace

    assert (await session.execute(select(GeoPlace))).scalars().all() == []  # завтра попробуем снова


async def test_order_city_is_rewritten_with_the_right_spelling(session, make_order, monkeypatch):
    _fake_nominatim(monkeypatch, {})
    _fake_photon(monkeypatch, [_photon("Острогожск", "Воронежская область", "Острогожский район", 50.87, 39.07)])
    order = await make_order(from_city="Острогоржск (Воронежская обл.)", to_city="Москва")
    from app.services.geocode import geocode_pending

    await geocode_pending()
    await session.refresh(order)

    assert order.from_city == "Острогожск (Воронежская обл.)"
    assert order.from_city_key == "острогожск (воронежская обл)"  # поисковые поля пересчитаны
    assert "острогожск" in order.search_text
    assert (round(order.from_lat, 2), round(order.from_lon, 2)) == (50.87, 39.07)
    from sqlalchemy import select

    from app.models import ActionLog

    log = (await session.execute(select(ActionLog).where(ActionLog.action == "city_corrected"))).scalar_one()
    assert "Острогоржск" in log.details and "Острогожск" in log.details


# --- Деревни, которых нет в OSM: ближайший известный пункт ------------------------------------------


async def test_village_is_attached_to_its_district_centre(session, monkeypatch):
    _fake_nominatim(monkeypatch, {})

    async def _area(query):
        return [{"addresstype": "district", "display_name": "Каховский район, Херсонская область", "lat": "46.8", "lon": "33.7"}]

    monkeypatch.setattr(geo, "_nominatim_area", _area)

    found = await geo.resolve(session, "Коробки (Каховский район)")

    assert [(c.kind, round(c.lat, 1)) for c in found] == [("near", 46.8)]
    assert "рядом с Каховский район" in found[0].name


async def test_district_centre_is_found_by_the_name_stem_through_photon(session, monkeypatch):
    """«Зарайский округ»: района в картах нет, но есть город Зарайск."""
    _fake_nominatim(monkeypatch, {})
    _fake_photon(monkeypatch, [_photon("Зарайск", "Московская область", "", 54.76, 38.88, "town")])

    found = await geo.resolve(session, "Мелкая (Зарайский округ)")

    assert [(c.kind, round(c.lat, 2)) for c in found] == [("near", 54.76)]


async def test_region_only_hint_gives_a_rough_region_point(session, monkeypatch):
    _fake_nominatim(monkeypatch, {})

    async def _area(query):
        return [{"addresstype": "state", "display_name": "Запорожская область, Украина", "lat": "47.4", "lon": "35.9"}]

    monkeypatch.setattr(geo, "_nominatim_area", _area)

    found = await geo.resolve(session, "Вершины (Запорожская обл.)")

    assert [c.kind for c in found] == ["region"]


async def test_region_guess_is_used_only_for_a_far_destination(session, make_order, monkeypatch):
    """Шереметьево -> Вершины: 1300 км, погрешность области незаметна; Москва -> СНТ в
    Подмосковье — центр области дал бы 0 км, такую привязку не ставим."""
    _fake_nominatim(monkeypatch, {})

    async def _area(query):
        centres = {"Запорожская область": ("47.4", "35.9"), "Московская область": ("55.6", "37.6")}
        lat, lon = centres[query]
        return [{"addresstype": "state", "display_name": f"{query}, Страна", "lat": lat, "lon": lon}]

    monkeypatch.setattr(geo, "_nominatim_area", _area)
    far = await make_order(from_city="Санкт-Петербург", to_city="Вершины (Запорожская обл.)")
    near = await make_order(from_city="Москва", to_city="Снт Дальний (Московская область)")
    from app.services.geocode import geocode_pending

    await geocode_pending()
    await session.refresh(far)
    await session.refresh(near)

    assert far.to_lat is not None
    assert near.to_lat is None  # честнее без координат, чем с расстоянием «0 км»


def test_misspelt_name_from_the_dictionary_is_spelled_the_standard_way():
    from app.city_aliases import canonical_city_name

    assert canonical_city_name("Станично-Луганское") == "Станица Луганская"


async def test_among_similar_names_only_the_closest_is_taken(session, monkeypatch):
    """«Кондоровка»: «Кондровка» похожа сильнее, чем «Кондуровка», — второе не берём."""
    _fake_nominatim(monkeypatch, {})
    _fake_photon(
        monkeypatch,
        [
            _photon("Кондуровка", "Оренбургская область", "Саракташский район", 51.5, 56.7, "hamlet"),
            _photon("Кондровка", "Белгородская область", "Прохоровский округ", 51.1, 37.0, "hamlet"),
        ],
    )

    found = await geo.resolve(session, "Кондоровка")

    assert [c.canonical for c in found] == ["Кондровка"]


# --- Ошибки, найденные на проде: переименование не должно угадывать ----------------------------------


async def test_one_letter_difference_in_a_short_name_is_not_a_typo(session, monkeypatch):
    """«Левинка» и «Левенка» (0,86) — возможно, разные деревни: не переименовываем."""
    _fake_nominatim(monkeypatch, {})
    _fake_photon(monkeypatch, [_photon("Левенка", "Брянская область", "Погарский район", 52.5, 33.1, "hamlet")])

    assert await geo.resolve(session, "Левинка (Брянская обл.)") == []


async def test_noticeably_different_name_is_not_a_typo(session, monkeypatch):
    """«Монастыри» -> «Монастырище» (0,8): другое место."""
    _fake_nominatim(monkeypatch, {})
    _fake_photon(monkeypatch, [_photon("Монастырище", "Черкасская область", "Монастыриский район", 49.0, 29.8, "town")])

    assert await geo.resolve(session, "Монастыри (Черкасская обл.)") == []


async def _geocode_pair(session, make_order, monkeypatch, other_city, **fields):
    from app.services.geocode import geocode_pending

    cities = {
        "Екатеринбург": Candidate(lat=56.84, lon=60.6, name="Екатеринбург, Свердловская область", kind="city"),
        "Москва": Candidate(lat=55.75, lon=37.62, name="Москва", kind="city"),
    }
    _fake_nominatim(monkeypatch, {other_city: [cities[other_city]]})
    order = await make_order(**fields)
    await session.commit()
    await geocode_pending()
    await session.refresh(order)
    return order


async def test_unverified_typo_far_from_the_other_end_is_dropped(session, make_order, monkeypatch):
    """Настоящий украинский «Кировоград» нельзя превратить в «Кировград» на Урале."""
    _fake_photon(monkeypatch, [_photon("Кировград", "Свердловская область", "Кировградский округ", 57.4, 60.1)])

    order = await _geocode_pair(session, make_order, monkeypatch, "Москва", from_city="Кировоград", to_city="Москва")

    assert order.from_city == "Кировоград"  # название не тронуто
    assert order.from_lat is None


async def test_unverified_typo_near_the_other_end_is_accepted(session, make_order, monkeypatch):
    """«Екатеринбург -> Кировоград»: едут явно в Кировград, он в 140 км."""
    _fake_photon(monkeypatch, [_photon("Кировград", "Свердловская область", "Кировградский округ", 57.4, 60.1)])

    order = await _geocode_pair(
        session, make_order, monkeypatch, "Екатеринбург", from_city="Кировоград", to_city="Екатеринбург"
    )

    assert order.from_city == "Кировград"
    assert order.from_lat is not None


# --- Кэш догадок сбрасывается при смене правил ---------------------------------------------------------


async def _cache(session, key, candidates, checked_at):
    from app.models import GeoPlace

    session.add(GeoPlace(key=key, status="ok", source="nominatim", candidates=candidates, checked_at=checked_at))
    await session.commit()


async def test_cached_guess_from_before_a_rule_change_is_recomputed(session, monkeypatch):
    from datetime import timedelta

    old = geo.GEO_LOGIC_DATE - timedelta(hours=1)
    guess = {"lat": 57.4, "lon": 60.1, "name": "Кировград, Свердловская область", "kind": "town", "canonical": "Кировград"}
    await _cache(session, geo.place_key("Кировоград", None), [guess], old)
    calls = _fake_nominatim(monkeypatch, {})

    found = await geo.resolve(session, "Кировоград")

    assert calls == ["Кировоград"]  # к новым правилам: кэшированная догадка не годится
    assert found == []


async def test_cached_exact_match_survives_a_rule_change(session, monkeypatch):
    from datetime import timedelta

    old = geo.GEO_LOGIC_DATE - timedelta(days=30)
    exact = {"lat": 55.0, "lon": 37.0, "name": "Подольск, Московская область", "kind": "city"}
    await _cache(session, geo.place_key("Подольск", None), [exact], old)
    calls = _fake_nominatim(monkeypatch, {})

    found = await geo.resolve(session, "Подольск")

    assert calls == [] and len(found) == 1  # точное совпадение от правил не зависит


async def test_cached_guess_made_by_current_rules_is_kept(session, monkeypatch):
    from datetime import timedelta

    fresh = geo.GEO_LOGIC_DATE + timedelta(seconds=1)
    guess = {"lat": 50.9, "lon": 39.1, "name": "Острогожск, Воронежская область", "kind": "town", "canonical": "Острогожск"}
    await _cache(session, geo.place_key("Острогоржск", "Воронежская область"), [guess], fresh)
    calls = _fake_nominatim(monkeypatch, {})

    found = await geo.resolve(session, "Острогоржск (Воронежская обл.)")

    assert calls == [] and found[0].canonical == "Острогожск"
