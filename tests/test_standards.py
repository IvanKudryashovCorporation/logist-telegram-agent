"""Единые названия городов, выбор места по цене за км, ссылка на диспетчера без username."""

import pytest

from app import city_aliases, geo, plausibility, routing
from app.city_aliases import CITY_ALIASES, canonical_city_name, expand_city_term
from app.db.base import SessionLocal
from app.geo import Candidate
from app.models import Order
from app.search import refresh_derived
from app.services import routes
from app.web import presenters, queries
from app.web.filters import Filters

# --- 1. Единое название города -----------------------------------------------------------------

MIN_VODY_SPELLINGS = [
    "Мин Воды", "Мин воды", "мин воды", "мин.воды", "Мин. Воды", "МИН-ВОДЫ", "Минводы", "минводы",
    "Минеральные Воды", "минеральные воды", "  Минеральные   Воды ", "г. Мин. Воды", "город Минеральные воды",
]


@pytest.mark.parametrize("spelling", MIN_VODY_SPELLINGS)
def test_every_spelling_of_a_city_gives_one_name(spelling):
    assert canonical_city_name(spelling) == "Минеральные Воды"
    assert expand_city_term(spelling) == "Минеральные Воды"


def test_every_alias_maps_to_its_canonical_name():
    """Защита от повторения: любая новая запись справочника должна работать при любом написании."""
    for alias, canonical in CITY_ALIASES.items():
        assert canonical_city_name(alias) == canonical, alias
        for variant in (alias.upper(), alias.title(), alias.replace(" ", "  "), alias.replace("-", " ")):
            assert canonical_city_name(variant) == canonical, (alias, variant)


def test_aliases_do_not_collide_between_different_cities():
    """Два разных города не должны сливаться в одну «сжатую» форму."""
    seen: dict[str, str] = {}
    for alias, canonical in CITY_ALIASES.items():
        key = city_aliases._compact(alias)
        assert seen.setdefault(key, canonical) == canonical, (alias, canonical, seen[key])


def test_unknown_cities_stay_as_written():
    assert canonical_city_name("Хлебодаровка (Волноваха)") is None
    assert expand_city_term("Хлебодаровка") == "Хлебодаровка"


def test_city_key_ignores_the_spelling_left_in_the_city_field():
    """В поле города мог остаться «Мин воды» — ключ поиска всё равно общий."""
    first = refresh_derived(Order(from_city="Мин Воды", to_city="Мин воды"))
    second = refresh_derived(Order(from_city="Минеральные Воды", to_city="минводы"))

    assert first.from_city_key == first.to_city_key == second.from_city_key == second.to_city_key


async def test_suggestions_have_one_entry_per_city(session, make_order):
    for spelling in ("Мин Воды", "Мин воды", "Минеральные Воды"):
        await make_order(from_city=spelling, to_city="Мелитополь")
    await make_order(from_city="мелитополь", to_city="Сочи")
    queries.invalidate_city_cache()

    cities = await queries.known_cities(session)

    assert cities.count("Минеральные Воды") == 1
    assert not [c for c in cities if city_aliases._compact(c) in {"минводы"}]
    mel = [c for c in cities if city_aliases._compact(c) == "мелитополь"]
    assert mel == ["Мелитополь"]
    assert len({city_aliases._compact(c) for c in cities}) == len(cities)  # ни одного дубля


@pytest.mark.parametrize("term", ["Мин воды", "Минеральные Воды", "минводы", "Мин. Воды"])
async def test_filter_finds_orders_whatever_spelling_was_used(session, make_order, term):
    stored_as = ["Мин Воды", "Минеральные Воды", "минводы"]
    ids = {(await make_order(from_city=name, to_city="Сочи")).id for name in stored_as}
    await make_order(from_city="Пятигорск", to_city="Сочи")  # чужой город — не должен попасть

    page = await queries.fetch_feed(session, filters=Filters(from_city=term), page=1, page_size=100)

    assert {o.id for o in page.items} == ids


# --- 2. Цена за км подсказывает, какое место верное ----------------------------------------------

KOTELNIKI = Candidate(55.66, 37.87, "Котельники, Московская область", "town")
ZHUKOVSKY_MOSCOW = Candidate(55.60, 38.12, "Жуковский, Московская область", "city")
ZHUKOVSKY_BRYANSK = Candidate(53.53, 33.73, "Жуковский, Брянская область", "town")


def test_choose_pair_prefers_a_plausible_price_per_km():
    pair = plausibility.choose_pair(30800, [ZHUKOVSKY_MOSCOW, ZHUKOVSKY_BRYANSK], [KOTELNIKI])

    assert pair == (ZHUKOVSKY_BRYANSK, KOTELNIKI)


def test_choose_pair_has_nothing_to_choose_from_with_single_candidates():
    assert plausibility.choose_pair(30800, [ZHUKOVSKY_MOSCOW], [KOTELNIKI]) is None


def test_choose_pair_refuses_when_no_pair_is_plausible():
    far = Candidate(10.0, 10.0, "Где-то", "village")
    assert plausibility.choose_pair(500, [far, far], [KOTELNIKI]) is None


async def test_route_worker_swaps_the_places_when_price_per_km_is_absurd(session, make_order, monkeypatch):
    """30 800 ₽ за 26 км: «Жуковский» оказался подмосковным, а речь о брянском."""
    async def fake_resolve(db, raw):
        return {"Жуковский": [ZHUKOVSKY_MOSCOW, ZHUKOVSKY_BRYANSK], "Котельники": [KOTELNIKI]}[raw]

    km_by_pair = {
        (ZHUKOVSKY_MOSCOW.coords, KOTELNIKI.coords): 26.0,
        (ZHUKOVSKY_BRYANSK.coords, KOTELNIKI.coords): 480.0,
    }

    async def fake_route(origin, destination, via=None):
        return km_by_pair[(origin, destination)]

    monkeypatch.setattr(geo, "resolve", fake_resolve)
    monkeypatch.setattr(routing, "road_distance_km", fake_route)
    order = await make_order(
        from_city="Жуковский", to_city="Котельники", price="30800",
        from_coords=ZHUKOVSKY_MOSCOW.coords, to_coords=KOTELNIKI.coords,
    )

    await routes.route_pending()

    async with SessionLocal() as fresh:
        row = await fresh.get(Order, order.id)
    assert row.distance_km == 480.0
    assert (row.from_lat, row.from_lon) == ZHUKOVSKY_BRYANSK.coords


async def test_plausible_orders_are_left_alone(session, make_order, monkeypatch):
    async def forbidden(db, raw):
        raise AssertionError("цена в порядке — места пересматривать не нужно")

    async def fake_route(origin, destination, via=None):
        return 200.0

    monkeypatch.setattr(geo, "resolve", forbidden)
    monkeypatch.setattr(routing, "road_distance_km", fake_route)
    order = await make_order(price="9000", from_coords=(55.0, 37.0), to_coords=(57.0, 37.0))

    await routes.route_pending()

    async with SessionLocal() as fresh:
        assert (await fresh.get(Order, order.id)).distance_km == 200.0


async def test_a_swap_that_does_not_help_is_not_applied(session, make_order, monkeypatch):
    async def fake_resolve(db, raw):
        return {"Жуковский": [ZHUKOVSKY_MOSCOW, ZHUKOVSKY_BRYANSK], "Котельники": [KOTELNIKI]}[raw]

    async def fake_route(origin, destination, via=None):
        return 26.0 if origin == ZHUKOVSKY_MOSCOW.coords else 3000.0  # и новая пара нереальна

    monkeypatch.setattr(geo, "resolve", fake_resolve)
    monkeypatch.setattr(routing, "road_distance_km", fake_route)
    order = await make_order(
        from_city="Жуковский", to_city="Котельники", price="30800",
        from_coords=ZHUKOVSKY_MOSCOW.coords, to_coords=KOTELNIKI.coords,
    )

    await routes.route_pending()

    async with SessionLocal() as fresh:
        row = await fresh.get(Order, order.id)
    assert (row.from_lat, row.from_lon) == ZHUKOVSKY_MOSCOW.coords
    assert row.distance_km == 26.0


# --- 3. Диспетчер без username ---------------------------------------------------------------------


def _order(**kwargs):
    base = dict(
        id=1, dispatcher_username=None, contact_username=None, dispatcher_tg_id=555,
        source_chat_id=-1001234567890, source_message_id=77,
    )
    base.update(kwargs)
    return Order(**base)


def test_message_link_for_supergroups():
    assert presenters.message_link(_order()) == "https://t.me/c/1234567890/77"


@pytest.mark.parametrize("chat_id", [None, 12345, -4242, -100])
def test_no_message_link_for_other_chats(chat_id):
    assert presenters.message_link(_order(source_chat_id=chat_id)) is None


def test_without_username_in_a_public_group_the_button_opens_the_message():
    link = presenters.dispatcher_link(_order(), text="Здравствуйте", group_username="pubgroup")

    assert link == "https://t.me/pubgroup/77"
    assert link.startswith("https://")  # а не tg://, который мобильные браузеры блокируют


def test_without_username_in_a_private_group_the_button_opens_the_chat_by_id():
    """Ссылка на сообщение закрытой группы пускает только участников, а водитель в ней не состоит."""
    assert presenters.dispatcher_link(_order()) == "tg://user?id=555"


def test_hidden_author_in_a_private_group_still_gets_the_message_link():
    link = presenters.dispatcher_link(_order(dispatcher_tg_id=None))

    assert link == "https://t.me/c/1234567890/77"


def test_username_still_wins_over_the_message_link():
    assert presenters.dispatcher_link(_order(dispatcher_username="disp")) == "https://t.me/disp"


def test_direct_link_is_the_last_resort():
    assert presenters.dispatcher_link(_order(source_chat_id=None)) == "tg://user?id=555"
    assert presenters.direct_chat_link(_order()) == "tg://user?id=555"
    assert presenters.dispatcher_link(_order(source_chat_id=None, dispatcher_tg_id=None)) is None


async def _author_without_username(make_order, session, *, public_group):
    from app.models import WorkGroup

    order = await make_order(dispatcher_username="")
    order.dispatcher_username = None
    order.dispatcher_tg_id = 555
    order.source_chat_id = -1001234567890
    if public_group:
        session.add(WorkGroup(tg_chat_id=-1001234567890, title="Публичная", username="pubgroup", watch_only=True))
    await session.commit()
    return order


async def test_public_group_page_offers_the_message_and_a_direct_chat_as_backup(client, make_order, session):
    order = await _author_without_username(make_order, session, public_group=True)

    page = await client.get(f"/orders/{order.id}")

    assert "нет публичного @username" in page.text
    assert "откроет его сообщение в группе" in page.text
    assert 'href="tg://user?id=555"' in page.text  # запасной вариант рядом
    assert "Открыть чат напрямую" in page.text


async def test_private_group_page_says_the_button_opens_a_personal_chat(client, make_order, session):
    order = await _author_without_username(make_order, session, public_group=False)

    page = await client.get(f"/orders/{order.id}")

    assert "откроет личный чат" in page.text
    assert "Открыть чат напрямую" not in page.text  # он и так главная кнопка
