"""Фильтр «Тип авто» (легковой / минивэн) вместо «Пассажиров»."""

import pytest

from app.search import vehicle_type_of
from app.web import queries
from app.web.filters import Filters

# --- Определение типа по заявке -----------------------------------------------------


@pytest.mark.parametrize(
    ("car_class", "raw_text", "passengers", "expected"),
    [
        ("минивэн", "", None, "minivan"),
        ("Минивен", "", 2, "minivan"),
        ("компактвэн", "", None, "minivan"),
        ("минивэн или компактвэн", "", None, "minivan"),
        ("Альфард", "", None, "minivan"),
        ("спринтер", "", None, "minivan"),
        ("вэн", "", None, "minivan"),
        (None, "03.10 13.00 Пермь Екатеринбург минивэн 18 000", None, "minivan"),
        (None, "Нужен микроавтобус на 8 человек", None, "minivan"),
        (None, "Брянск - Москва", 5, "minivan"),
        (None, "Брянск - Москва", 7, "minivan"),
        ("комфорт", "", 4, "car"),
        ("бизнес", "", None, "car"),
        ("Комфорт+", "Симферополь Сочи 14000", 2, "car"),
        (None, "Симферополь Сочи 14000", None, "car"),
        # Слово «вэн» внутри другого слова минивэном не считается.
        (None, "Ивэнтовая улица", None, "car"),
    ],
)
def test_vehicle_type_of(car_class, raw_text, passengers, expected):
    assert vehicle_type_of(car_class, raw_text, passengers) == expected


# --- Фильтр -----------------------------------------------------------------------


async def _feed(session, vehicle):
    page = await queries.fetch_feed(session, filters=Filters(vehicle=vehicle), page=1, page_size=100)
    return {order.id for order in page.items}


async def test_filter_splits_cars_and_minivans(session, make_order):
    car = await make_order(passengers=2)
    comfort = await make_order(passengers=None, raw_text="Сочи Адлер 5000 комфорт")
    minivan = await make_order(passengers=2, raw_text="Пермь Екатеринбург минивэн 18000")
    seven = await make_order(passengers=7)

    assert await _feed(session, "") == {car.id, comfort.id, minivan.id, seven.id}
    assert await _feed(session, "car") == {car.id, comfort.id}
    assert await _feed(session, "minivan") == {minivan.id, seven.id}


async def test_filter_matches_python_reference(session, make_order):
    await make_order(passengers=2)
    await make_order(passengers=6)
    page = await queries.fetch_feed(session, filters=Filters(), page=1, page_size=100)

    for vehicle in ("car", "minivan"):
        filters = Filters(vehicle=vehicle)
        by_sql = await _feed(session, vehicle)
        by_python = {order.id for order in page.items if filters.matches(order)}
        assert by_sql == by_python, vehicle


def test_unknown_vehicle_value_is_ignored():
    assert Filters(vehicle="грузовик").vehicle == ""
    assert Filters(vehicle=" MINIVAN ").vehicle == "minivan"
    assert Filters(vehicle="car").active_count == 1
    assert Filters().active_count == 0


def test_legacy_passengers_parameter_is_accepted_and_ignored():
    """Старые ссылки и сохранённые подписки не должны падать."""
    filters = Filters(from_city="Сочи", passengers="3")

    assert filters.active_count == 1
    assert "passengers" not in filters.as_dict()
    assert filters.as_dict()["vehicle"] == ""


async def test_order_gets_its_vehicle_type_on_save(session, make_order):
    order = await make_order(passengers=2, raw_text="Пермь Екатеринбург минивэн")

    assert order.vehicle_type == "minivan"
    assert (await make_order(passengers=2)).vehicle_type == "car"


# --- Сайт ---------------------------------------------------------------------------


async def test_site_filters_by_vehicle(client, make_order):
    car = await make_order(from_city="Сочи", passengers=2)
    van = await make_order(from_city="Сочи", passengers=6)

    minivans = await client.get("/?vehicle=minivan")
    cars = await client.get("/?vehicle=car")

    assert f"/orders/{van.id}" in minivans.text and f"/orders/{car.id}" not in minivans.text
    assert f"/orders/{car.id}" in cars.text and f"/orders/{van.id}" not in cars.text


async def test_filter_form_has_vehicle_select_instead_of_passengers(client, make_order):
    await make_order()

    page = await client.get("/?vehicle=minivan")

    assert "Тип авто" in page.text
    assert 'value="minivan" selected' in page.text
    assert "Легковой" in page.text and "Минивэн" in page.text
    assert "Пассажиров (мин.)" not in page.text
    assert 'name="passengers"' not in page.text


async def test_old_passengers_link_still_opens(client, make_order):
    await make_order()

    assert (await client.get("/?passengers=3")).status_code == 200
