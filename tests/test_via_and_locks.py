"""Остановки в маршруте, закрытые замки и плашка «Уточняется» (заявки с прода).

* «Пермь - Соликамск - Пермь Аэропорт»: остановка видна, расстояние — по всему пути;
* 🔒 в тексте — заказ взяли, его надо снять, а 🔓 — свободно;
* «Уточняется» только когда нет города или цены, а не из-за «пассажиров 1-2» и «28000+ платка».
"""

from datetime import timedelta

import pytest
from sqlalchemy import select

from app import geo, routing
from app.geo import Candidate
from app.models import ActionLog, Order, OrderStatus
from app.parsing.closed import has_closed_lock
from app.parsing.llm_parser import ParseResult
from app.parsing.schema import ParsedOrder
from app.services.cleanup import resolve_clarifications
from app.services.geocode import geocode_pending
from app.services.routes import route_pending
from app.telegram import work_group
from app.timeutil import now_msk_naive

PERM = (58.0105, 56.2502)
SOLIKAMSK = (59.6338, 56.7742)
AIRPORT = (57.9145, 56.0212)


def _parse(monkeypatch, *orders: dict):
    async def fake_parse_orders(text: str) -> ParseResult:
        return ParseResult(orders=[ParsedOrder(**fields) for fields in orders])

    monkeypatch.setattr(work_group, "parse_orders", fake_parse_orders)
    monkeypatch.setattr(work_group.settings, "prefilter_enabled", False)


async def _post(message_id, text="заявка", chat_id=-100777, is_edit=False):
    return await work_group.upsert_order_text(
        chat_id=chat_id, message_id=message_id, text=text, dispatcher_tg_id=5, dispatcher_username="disp",
        is_edit=is_edit,
    )


async def _orders(session):
    session.expire_all()
    return (await session.execute(select(Order).order_by(Order.id))).scalars().all()


ROUTE = {"from_city": "Пермь", "to_city": "Пермь Аэропорт", "via_points": ["Соликамск"], "client_price": 13500,
         "pickup_date": "2026-10-08", "pickup_time": "11:00"}


# --- Остановки -----------------------------------------------------------------------------------------


async def test_stops_are_saved_in_order_and_searchable(session, monkeypatch):
    _parse(monkeypatch, {**ROUTE, "via_points": ["Соликамск", " ", "Березники"]})

    await _post(1)

    (order,) = await _orders(session)
    assert [p["name"] for p in order.via_points] == ["Соликамск", "Березники"]  # пустые отброшены
    assert "соликамск" in order.search_text and "березники" in order.search_text


async def test_order_without_stops_has_none(session, monkeypatch):
    _parse(monkeypatch, {**ROUTE, "via_points": []})

    await _post(1)

    assert (await _orders(session))[0].via_points is None


async def test_changed_stops_reset_the_distance_but_same_stops_keep_coordinates(session, monkeypatch):
    _parse(monkeypatch, ROUTE)
    await _post(1)
    (order,) = await _orders(session)
    order.via_points = [{"name": "Соликамск", "lat": 59.6, "lon": 56.8}]
    order.distance_km, order.route_checked_at = 400.0, now_msk_naive()
    session.add(order)
    await session.commit()

    await _post(1, is_edit=True)  # правка, остановки те же
    assert (await _orders(session))[0].via_points[0]["lat"] == 59.6

    _parse(monkeypatch, {**ROUTE, "via_points": ["Березники"]})
    await _post(1, is_edit=True)  # остановка другая
    changed = (await _orders(session))[0]
    assert changed.via_points[0]["name"] == "Березники" and changed.via_points[0]["lat"] is None
    assert changed.distance_km is None and changed.route_checked_at is None


async def test_stop_is_geocoded_near_the_previous_point(session, make_order, monkeypatch):
    calls = []

    async def fake_resolve(db, raw):
        calls.append(raw)
        table = {"Пермь": PERM, "Пермь Аэропорт": AIRPORT, "Соликамск": SOLIKAMSK}
        return [Candidate(lat=table[raw][0], lon=table[raw][1], name=raw, kind="city")]

    monkeypatch.setattr(geo, "resolve", fake_resolve)
    order = await make_order(from_city="Пермь", to_city="Пермь Аэропорт")
    order.via_points = [{"name": "Соликамск", "lat": None, "lon": None}]
    session.add(order)
    await session.commit()

    await geocode_pending()

    (done,) = await _orders(session)
    assert (done.via_points[0]["lat"], done.via_points[0]["lon"]) == SOLIKAMSK
    assert calls == ["Пермь", "Пермь Аэропорт", "Соликамск"]


async def test_unfound_stop_stays_visible_without_coordinates(session, make_order, monkeypatch):
    async def fake_resolve(db, raw):
        return [Candidate(lat=PERM[0], lon=PERM[1], name=raw, kind="city")] if raw != "Дальняя" else []

    monkeypatch.setattr(geo, "resolve", fake_resolve)
    order = await make_order(from_city="Пермь", to_city="Кунгур")
    order.via_points = [{"name": "Дальняя", "lat": None, "lon": None}]
    session.add(order)
    await session.commit()

    await geocode_pending()

    (done,) = await _orders(session)
    assert done.via_points == [{"name": "Дальняя", "lat": None, "lon": None}]


async def test_distance_goes_through_the_stops_even_for_a_round_trip(session, make_order, monkeypatch):
    """Начало и конец в одном месте, но путь — туда и обратно через Соликамск."""
    seen = []

    async def fake_road(origin, destination, via=()):
        seen.append((origin, destination, list(via)))
        return 430.0

    monkeypatch.setattr(routing, "road_distance_km", fake_road)
    order = await make_order(from_coords=PERM, to_coords=PERM, from_city="Пермь", to_city="Пермь Аэропорт")
    order.via_points = [{"name": "Соликамск", "lat": SOLIKAMSK[0], "lon": SOLIKAMSK[1]}]
    session.add(order)
    await session.commit()

    await route_pending()

    (done,) = await _orders(session)
    assert done.distance_km == 430.0
    assert seen == [(PERM, PERM, [SOLIKAMSK])]


def test_sanity_check_uses_the_whole_chain_not_just_the_ends():
    # Концы совпадают (прямая 0), но по цепочке Пермь → Соликамск → Пермь около 350 км.
    assert routing.check_km(430.0, PERM, PERM, [SOLIKAMSK]) == 430.0
    assert routing.check_km(430.0, PERM, PERM) == 0.0  # без остановок путь «туда-обратно» схлопывается в 0
    assert routing.check_km(100.0, PERM, PERM, [SOLIKAMSK]) is None  # короче прямой по цепочке — ошибка


async def test_osrm_url_contains_every_point(monkeypatch):
    urls = []

    class FakeClient:
        def __init__(self, **kwargs): ...
        async def __aenter__(self): return self
        async def __aexit__(self, *exc): return False

        async def get(self, url, params=None, headers=None):
            import httpx

            urls.append(url)
            return httpx.Response(200, json={"code": "Ok", "routes": [{"distance": 430000}]})

    monkeypatch.setattr(routing.httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(routing, "_MIN_INTERVAL", 0)

    km = await routing.road_distance_km(PERM, AIRPORT, [SOLIKAMSK])

    assert km == 430.0
    assert urls[0].endswith("56.250200,58.010500;56.774200,59.633800;56.021200,57.914500")


async def test_donor_distance_is_not_used_for_an_order_with_stops(session, make_order, monkeypatch):
    async def fake_road(origin, destination, via=()):
        return 430.0 if via else 20.0

    monkeypatch.setattr(routing, "road_distance_km", fake_road)
    plain = await make_order(from_coords=PERM, to_coords=AIRPORT)
    plain.distance_km = 20.0
    session.add(plain)
    stopped = await make_order(from_coords=PERM, to_coords=AIRPORT)
    stopped.via_points = [{"name": "Соликамск", "lat": SOLIKAMSK[0], "lon": SOLIKAMSK[1]}]
    await session.commit()

    await route_pending()

    done = {o.id: o.distance_km for o in await _orders(session)}
    assert done[stopped.id] == 430.0


async def test_card_and_text_show_the_stops(client, make_order, session):
    from app.web.presenters import dispatcher_message, route_text

    order = await make_order(from_city="Пермь", to_city="Пермь Аэропорт")
    order.via_points = [{"name": "Соликамск", "lat": None, "lon": None}]
    session.add(order)
    await session.commit()

    assert route_text(order) == "Пермь → Соликамск → Пермь Аэропорт"
    assert "Пермь → Соликамск → Пермь Аэропорт" in dispatcher_message(order)
    assert "Соликамск" in (await client.get("/")).text
    assert "Остановка 1" in (await client.get(f"/orders/{order.id}")).text


async def test_notification_text_shows_the_stops(make_order):
    from app.services.subscriptions import order_line

    order = await make_order(from_city="Пермь", to_city="Пермь Аэропорт")
    order.via_points = [{"name": "Соликамск", "lat": None, "lon": None}]

    assert order_line(order).startswith("Пермь → Соликамск → Пермь Аэропорт")


# --- Замки --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("text,closed", [
    ("Пермь - Москва 5000 🔒", True), ("28000+ платка. 🔐🔐🔐", True), ("Пермь - Москва 5000 🔓", False),
    ("Обычный текст", False), ("", False), (None, False),
])
def test_closed_lock_detection(text, closed):
    assert has_closed_lock(text) is closed


async def test_edit_with_a_closed_lock_removes_the_order(session, monkeypatch):
    _parse(monkeypatch, ROUTE)
    await _post(1, "Пермь - Соликамск 13500")
    assert (await _orders(session))[0].status == OrderStatus.NEW

    await _post(1, "Пермь - Соликамск 13500 🔒", is_edit=True)

    (order,) = await _orders(session)
    assert order.status == OrderStatus.CANCELLED
    log = (await session.execute(select(ActionLog).where(ActionLog.action == "cancelled_closed_lock"))).scalar_one()
    assert log.order_id == order.id


async def test_open_lock_changes_nothing(session, monkeypatch):
    _parse(monkeypatch, ROUTE)
    await _post(1, "Пермь - Соликамск 13500")

    await _post(1, "Пермь - Соликамск 13500 🔓", is_edit=True)

    assert (await _orders(session))[0].status == OrderStatus.NEW


async def test_new_message_with_a_closed_lock_is_not_created(session, monkeypatch):
    _parse(monkeypatch, ROUTE)

    await _post(1, "Пермь - Соликамск 13500 🔒")

    assert await _orders(session) == []


async def test_order_taken_by_a_driver_survives_the_lock(session, monkeypatch):
    _parse(monkeypatch, ROUTE)
    await _post(1, "Пермь - Соликамск 13500")
    (order,) = await _orders(session)
    order.taken_by_token, order.status = "tg:1", OrderStatus.AGREED
    session.add(order)
    await session.commit()

    await _post(1, "Пермь - Соликамск 13500 🔒", is_edit=True)

    kept = (await _orders(session))[0]
    assert kept.status == OrderStatus.AGREED and kept.taken_by_token == "tg:1"


async def test_in_a_list_only_the_locked_order_is_removed(session, monkeypatch):
    first = {**ROUTE, "to_city": "Березники", "via_points": [], "raw_snippet": "Пермь - Березники 3000"}
    second = {**ROUTE, "to_city": "Кунгур", "via_points": [], "raw_snippet": "Пермь - Кунгур 2000"}
    _parse(monkeypatch, first, second)
    await _post(1, "Пермь - Березники 3000\nПермь - Кунгур 2000")
    assert len(await _orders(session)) == 2

    first["raw_snippet"] = "Пермь - Березники 3000 🔒"  # закрыта первая, вторая открыта
    _parse(monkeypatch, first, second)
    await _post(1, "Пермь - Березники 3000 🔒\nПермь - Кунгур 2000", is_edit=True)

    statuses = [o.status for o in await _orders(session)]
    assert statuses == [OrderStatus.CANCELLED, OrderStatus.NEW]


async def test_lock_removes_the_telegram_notification_too(session, make_order):
    from app.models import ActorType, OrderSubscription, SubscriptionNotification
    from app.services.subscriptions import OK, retract_withdrawn
    from app.timeutil import now_utc_naive

    sub = OrderSubscription(telegram_id=1001, params={"from_city": "Пермь"}, is_active=True, since=now_utc_naive())
    session.add(sub)
    order = await make_order(from_city="Пермь")
    await session.commit()
    session.add(SubscriptionNotification(subscription_id=sub.id, order_id=order.id, sent_at=now_utc_naive(),
                                         message_id=501))
    order.status = OrderStatus.CANCELLED
    session.add(order)
    session.add(ActionLog(order_id=order.id, actor=ActorType.DISPATCHER, action="cancelled_closed_lock"))
    await session.commit()
    deleted = []

    async def delete(chat_id, message_id):
        deleted.append((chat_id, message_id))
        return OK

    assert await retract_withdrawn(delete=delete) == 1
    assert deleted == [(1001, 501)]


# --- «Уточняется» -------------------------------------------------------------------------------------


@pytest.mark.parametrize("missing", [
    ["точное число пассажиров (указано 1-2)"], ["не указана точная стоимость"], ["нет адреса отправления"],
])
async def test_vague_details_no_longer_make_an_order_unclear(session, monkeypatch, missing):
    _parse(monkeypatch, {**ROUTE, "via_points": [], "missing_fields": missing})

    await _post(1)

    assert (await _orders(session))[0].status == OrderStatus.NEW


@pytest.mark.parametrize("gap", [{"client_price": None}, {"to_city": None}, {"from_city": None}])
async def test_missing_route_or_price_still_needs_clarification(session, monkeypatch, gap):
    _parse(monkeypatch, {**ROUTE, "via_points": [], **gap})

    await _post(1)

    assert (await _orders(session))[0].status == OrderStatus.NEEDS_CLARIFICATION


async def test_cleanup_clears_the_badge_on_orders_that_have_route_and_price(session, make_order):
    usable = await make_order(status=OrderStatus.NEEDS_CLARIFICATION, pickup_at=now_msk_naive() + timedelta(days=1))
    no_price = await make_order(status=OrderStatus.NEEDS_CLARIFICATION, price="")
    no_price.client_price = None
    session.add(no_price)
    await session.commit()

    assert await resolve_clarifications() == 1

    by_id = {o.id: o.status for o in await _orders(session)}
    assert by_id[usable.id] == OrderStatus.NEW and by_id[no_price.id] == OrderStatus.NEEDS_CLARIFICATION
