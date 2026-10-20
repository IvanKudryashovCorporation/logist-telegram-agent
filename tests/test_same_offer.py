"""Страховка: один рейс, опубликованный по-разному (другой диспетчер, текст, написание места), склеивается фоновой уборкой."""

from datetime import datetime, timedelta

from sqlalchemy import select

from app.db.base import SessionLocal
from app.dedupe_rules import LiveOrder, duplicate_ids, is_same_offer
from app.models import Order
from app.services.dedupe import cancel_duplicate_orders
from app.timeutil import now_msk_naive

WHEN = datetime(2026, 10, 21, 16, 30)


def _live(id_, from_city, to_city, **kw):
    base = dict(tg_id=id_, username=f"dispatcher_{id_}", pickup_at=WHEN, phone=None, passengers=2,
                created_at=WHEN - timedelta(hours=1), raw_text=f"совсем другой текст {id_}", price=7000.0)
    base.update(kw)
    return LiveOrder(id_, from_city=from_city, to_city=to_city, **base)


def test_airport_and_its_city_are_the_same_offer():
    assert is_same_offer(_live(1, "Рязань", "Шереметьево"), _live(2, "Рязань", "Москва"))
    assert duplicate_ids([_live(1, "Рязань", "Шереметьево"), _live(2, "Рязань", "Москва")]) == [1]


def test_places_close_by_coordinates_are_the_same_place():
    older = _live(1, "Ликино дулево", "Кондрово", from_coords=(55.70, 38.96))
    newer = _live(2, "Ликино-Дулёво (МО)", "Кондрово", from_coords=(55.71, 38.97))
    assert is_same_offer(older, newer)


def test_different_price_time_or_far_place_is_a_different_offer():
    base = _live(1, "Рязань", "Шереметьево")
    assert not is_same_offer(base, _live(2, "Рязань", "Москва", price=8000.0))  # другая цена
    assert not is_same_offer(base, _live(2, "Рязань", "Москва", pickup_at=WHEN + timedelta(hours=6)))  # другое время
    far = _live(2, "Рязань", "Тула", to_coords=(54.19, 37.62))
    assert not is_same_offer(_live(1, "Рязань", "Москва", to_coords=(55.75, 37.62)), far)  # далеко друг от друга


def test_clearly_different_clients_are_not_merged():
    assert not is_same_offer(_live(1, "Рязань", "Шереметьево", phone="+79990000001"),
                             _live(2, "Рязань", "Москва", phone="+79990000002"))
    assert not is_same_offer(_live(1, "Рязань", "Шереметьево", passengers=2), _live(2, "Рязань", "Москва", passengers=5))


async def test_background_pass_merges_airport_and_city(session, make_order):
    pickup = now_msk_naive() + timedelta(days=1)
    older = await make_order(from_city="Рязань", to_city="Шереметьево", price="7000", pickup_at=pickup,
                             dispatcher_username="first", client_phone=None, passengers=2,
                             raw_text="Рязань - Шереметьево завтра 2 чел 7000")
    newer = await make_order(from_city="Рязань", to_city="Москва", price="7000", pickup_at=pickup,
                             dispatcher_username="second", client_phone=None, passengers=2,
                             raw_text="Завтра из Рязани в Москву, двое, семь тысяч")
    newer.dispatcher_tg_id = 222
    older.to_address = newer.to_address = None
    await session.commit()

    assert await cancel_duplicate_orders() == 1

    async with SessionLocal() as fresh:
        statuses = {o.id: o.status.value for o in (await fresh.execute(select(Order).where(Order.id.in_([older.id, newer.id])))).scalars()}
    assert statuses == {older.id: "cancelled", newer.id: "new"}  # остаётся самый новый, со свежими данными
