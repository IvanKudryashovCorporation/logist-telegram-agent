"""Одно и то же сообщение: тексты могут отличаться дописанной строкой."""

from datetime import datetime

from app.dedupe_rules import LiveOrder, is_repost

BASE = (
    "Сейчас +-один час\n"
    "Откуда Сатка Челябинская область\n"
    "Куда село Париж Челябинская область\n"
    "Два взрослых и один ребёнок\n"
    "Растояние 343\n"
    "Водителю на руки 10000"
)
NOW = datetime(2026, 10, 4, 14, 0)


def _order(order_id, text):
    return LiveOrder(order_id, None, None, "Сатка", "село Париж", None, None, None, NOW, text)


def test_a_line_added_in_one_group_does_not_make_it_a_different_message():
    assert is_repost(_order(1, BASE + "\nНет машины"), _order(2, BASE))


def test_a_different_message_on_the_same_route_is_not_a_repost():
    other = "Сейчас, нужен минивэн, пять человек, багаж большой, Сатка - Париж"

    assert not is_repost(_order(1, BASE), _order(2, other))


def test_very_short_texts_are_never_compared():
    assert not is_repost(_order(1, "Сатка"), _order(2, "Сатка"))


def test_texts_that_differ_in_numbers_are_different_offers():
    cheaper = BASE.replace("10000", "9000")

    assert not is_repost(_order(1, BASE), _order(2, cheaper))


def test_reverse_trip_inside_one_city_is_not_a_repost():
    kwargs = dict(tg_id=7, username="d", pickup_at=NOW, phone=None, passengers=1, created_at=NOW,
                  raw_text="06.10 в 06:30 Краснодар - Краснодар 1 чел 1000")
    there = LiveOrder(1, from_city="Краснодар", to_city="Краснодар", from_address="ЖД вокзал", to_address="аэропорт", **kwargs)
    back = LiveOrder(2, from_city="Краснодар", to_city="Краснодар", from_address="аэропорт", to_address="ЖД вокзал", **kwargs)
    again = LiveOrder(3, from_city="Краснодар", to_city="Краснодар", from_address="ЖД вокзал", to_address="аэропорт", **kwargs)

    assert not is_repost(there, back)
    assert is_repost(there, again)
