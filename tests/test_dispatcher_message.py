"""Готовое сообщение диспетчеру и ссылка t.me с подставленным текстом."""

from datetime import datetime
from decimal import Decimal
from urllib.parse import unquote

from app.models import Order
from app.web.presenters import dispatcher_link, dispatcher_message


def _order(**overrides) -> Order:
    fields = dict(
        id=7,
        from_city="Краснодар",
        to_city="Сочи",
        pickup_at=datetime(2026, 10, 5, 14, 0),
        client_price=Decimal("3000"),
        client_phone="+79990000000",
        client_name="Иван",
        dispatcher_username="disp",
        dispatcher_tg_id=111,
    )
    fields.update(overrides)
    return Order(**fields)


def test_full_message():
    assert dispatcher_message(_order()) == (
        "Здравствуйте! Заказ Краснодар → Сочи, 05.10 в 14:00, 3000 ₽ — актуально?"
    )


def test_message_without_price():
    message = dispatcher_message(_order(client_price=None))
    assert message == "Здравствуйте! Заказ Краснодар → Сочи, 05.10 в 14:00 — актуально?"


def test_message_without_time():
    message = dispatcher_message(_order(pickup_at=None))
    assert message == "Здравствуйте! Заказ Краснодар → Сочи, в ближайшее время, 3000 ₽ — актуально?"


def test_message_route_only():
    message = dispatcher_message(_order(pickup_at=None, client_price=None))
    assert message == "Здравствуйте! Заказ Краснодар → Сочи, в ближайшее время — актуально?"


def test_message_falls_back_to_addresses_then_id():
    with_addresses = dispatcher_message(
        _order(from_city=None, to_city=None, from_address="ул. Ленина 1", to_address="аэропорт")
    )
    assert "ул. Ленина 1 → аэропорт" in with_addresses

    nothing = dispatcher_message(
        _order(from_city=None, to_city=None, pickup_at=None, client_price=None)
    )
    assert nothing == "Здравствуйте! Заказ №7, в ближайшее время — актуально?"


def test_message_never_contains_client_contacts():
    message = dispatcher_message(_order())
    assert "+79990000000" not in message
    assert "Иван" not in message


def test_link_carries_encoded_text():
    link = dispatcher_link(_order(), text="Здравствуйте! A → B, 5 ₽?")

    assert link.startswith("https://t.me/disp?text=")
    assert "Здравствуйте" not in link  # кириллица и пробелы закодированы
    assert " " not in link
    assert unquote(link.split("?text=", 1)[1]) == "Здравствуйте! A → B, 5 ₽?"


def test_link_without_text_is_plain():
    assert dispatcher_link(_order()) == "https://t.me/disp"


def test_contact_username_beats_sender():
    link = dispatcher_link(_order(contact_username="real_boss"), text="привет")
    assert link.startswith("https://t.me/real_boss?text=")


def test_tg_id_fallback_has_no_text():
    link = dispatcher_link(_order(dispatcher_username=None), text="привет")
    assert link == "tg://user?id=111"


def test_no_account_gives_no_link():
    assert dispatcher_link(_order(dispatcher_username=None, dispatcher_tg_id=None), text="x") is None
