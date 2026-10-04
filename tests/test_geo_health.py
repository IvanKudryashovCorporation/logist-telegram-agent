"""Корневая причина «нет расстояния / неверное расстояние»: отчёт, регион из адреса,
версия геокодера."""

from app import geo
from app.geo import Candidate
from app.models import Order
from app.services import geo_health
from app.services.geocode import geocode_pending
from app.timeutil import now_utc_naive

ASTRAKHAN = (46.35, 48.03)
UFA = (54.74, 55.97)


def _fake_nominatim(monkeypatch, answers):
    calls = []

    async def _search(query):
        calls.append(query)
        return answers.get(query, [])

    monkeypatch.setattr(geo, "_nominatim_search", _search)
    return calls


# --- Регион из адреса -------------------------------------------------------------


def test_region_hint_from_address():
    assert geo.region_hint("Ля Дача, Астраханская область, Лиманский район") == "Астраханская область"
    assert geo.region_hint("ул. Ленина 5, Краснодарский край") == "Краснодарский край"
    assert geo.region_hint("Республика Крым") == "Республика Крым"
    assert geo.region_hint("новоайдарский район") == "Новоайдарский район"  # район тоже сужает поиск
    assert geo.region_hint("Аэропорт") is None
    assert geo.region_hint(None) is None


async def test_region_from_address_fixes_the_wrong_village(session, make_order, monkeypatch):
    """Заказ 844: «село Вышка», адрес в Астраханской области, а Nominatim без
    региона отдаёт Вышку в Закарпатье (2335 км от Астрахани)."""
    zakarpattia = Candidate(48.94, 22.69, "Вышка, Костринська сільська громада, Закарпатська область", "village")
    astrakhan_vyshka = Candidate(45.61, 47.64, "Вышка, Лиманский муниципальный округ, Астраханская область", "village")
    calls = _fake_nominatim(
        monkeypatch,
        {"Вышка": [zakarpattia], "Вышка, Астраханская область": [astrakhan_vyshka]},
    )
    order = await make_order(from_city="Астрахань", to_city="село Вышка")
    order.to_address = "Ля Дача, Астраханская область, Лиманский район"
    await session.commit()

    await geocode_pending()
    await session.refresh(order)

    assert (order.to_lat, order.to_lon) == astrakhan_vyshka.coords
    assert "Вышка, Астраханская область" in calls


async def test_region_lookup_also_rescues_a_place_not_found_at_all(session, make_order, monkeypatch):
    found = Candidate(45.0, 40.0, "Рассветная, Краснодарский край", "village")
    _fake_nominatim(monkeypatch, {"Рассветная, Краснодарский край": [found]})
    order = await make_order(from_city="Рассветная", to_city="Краснодар")
    order.from_address = "Краснодарский край"
    await session.commit()

    await geocode_pending()
    await session.refresh(order)

    assert (order.from_lat, order.from_lon) == found.coords


async def test_city_with_its_own_region_is_not_given_another(session, make_order, monkeypatch):
    """«Брянка ЛНР» уже содержит регион — подсказку из адреса не навязываем."""
    calls = _fake_nominatim(monkeypatch, {"Брянка, Луганская область": [Candidate(48.5, 38.6, "Брянка", "town")]})
    order = await make_order(from_city="Брянка ЛНР", to_city="Краснодар")
    order.from_address = "Краснодарский край"
    await session.commit()

    await geocode_pending()

    assert calls == ["Брянка, Луганская область"]


# --- Версия геокодера -------------------------------------------------------------


async def test_orders_geocoded_by_an_old_version_are_redone(session, make_order, monkeypatch):
    """Подняли GEO_VERSION — воркер сам пересчитывает старые заказы, вручную сбрасывать не нужно."""
    _fake_nominatim(monkeypatch, {})
    order = await make_order(from_coords=ASTRAKHAN, to_coords=UFA)
    order.geo_version = geo.GEO_VERSION - 1
    await session.commit()

    assert await geocode_pending() == 1
    await session.refresh(order)
    assert order.geo_version == geo.GEO_VERSION
    assert await geocode_pending() == 0  # второй раз не трогаем


async def test_orders_without_version_are_redone(session, make_order, monkeypatch):
    _fake_nominatim(monkeypatch, {})
    order = await make_order(from_coords=ASTRAKHAN, to_coords=UFA)
    order.geo_version = None
    await session.commit()

    assert await geocode_pending() == 1


# --- Отчёт ----------------------------------------------------------------------


def _checked(**kwargs) -> Order:
    return Order(geo_checked_at=now_utc_naive(), **kwargs)


def test_classify_ok_and_waiting():
    ok = _checked(from_city="А", to_city="Б", from_lat=1.0, from_lon=1.0, to_lat=2.0, to_lon=2.0,
                  distance_km=200.0, client_price=8000)
    waiting = _checked(from_city="А", to_city="Б", from_lat=1.0, from_lon=1.0, to_lat=2.0, to_lon=2.0)
    not_yet = Order(from_city="А", to_city="Б")

    assert geo_health.classify(ok) is None
    assert geo_health.classify(waiting) is None
    assert geo_health.classify(not_yet) is None


def test_classify_problems():
    no_coords = _checked(from_city="Гречишкино", to_city="Ижевск", from_lat=None, to_lat=56.8, to_lon=53.2)
    same = _checked(from_city="Уфа", to_city="Стерлитамак", from_lat=54.7, from_lon=55.9, to_lat=54.7,
                    to_lon=55.9, route_checked_at=now_utc_naive())
    no_route = _checked(from_city="А", to_city="Б", from_lat=1.0, from_lon=1.0, to_lat=2.0, to_lon=2.0,
                        route_checked_at=now_utc_naive())
    odd = _checked(from_city="Астрахань", to_city="село Вышка", from_lat=46.3, from_lon=48.0,
                   to_lat=48.9, to_lon=22.7, distance_km=2335.0, client_price=2000)

    assert geo_health.classify(no_coords) == ("no_coords", "«Гречишкино»")
    assert geo_health.classify(same)[0] == "same_point"
    assert geo_health.classify(no_route)[0] == "no_route"
    assert geo_health.classify(odd)[0] == "odd_rate"


async def test_admin_dashboard_lists_distance_problems(client, make_order, session, monkeypatch):
    from app.config import settings
    from tests.test_admin_web import BOT_TOKEN, _login  # вход владельца через Telegram

    monkeypatch.setattr(settings, "telegram_login_bot_token", BOT_TOKEN)
    monkeypatch.setattr(settings, "telegram_login_bot_username", "podacha_bot")
    order = await make_order(from_coords=ASTRAKHAN, to_coords=(48.9, 22.7))
    order.distance_km = 2335.0
    order.client_price = 2000
    await session.commit()
    await _login(client)

    page = await client.get("/admin")

    assert 'id="distance-issues"' in page.text
    assert f"/orders/{order.id}" in page.text
    assert "подозрительная цена за км" in page.text
