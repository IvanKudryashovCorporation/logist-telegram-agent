"""Цена, которую модель не вернула, добирается из текста заявки (баг «30000 водителю» без цены)."""

from decimal import Decimal

import pytest

from app.parsing.llm_parser import fill_missing_prices
from app.parsing.price import extract_price
from app.parsing.schema import ParsedOrder


@pytest.mark.parametrize(
    "text, expected",
    [
        ("Сейчас срочно 🚨\n\nКурск\n\nТольятти\n\n30000 водителю\n\n1 пассажир", Decimal(30000)),
        ("Курск - Тольятти 30 000 водителю", Decimal(30000)),
        ("Москва Тула\nводителю - 12000", Decimal(12000)),
        ("Адлер Сочи цена 4500", Decimal(4500)),
        ("Адлер Сочи сумма: 25к", Decimal(25000)),
        ("Симферополь Ялта 7.5к вод", Decimal(7500)),
        ("Керчь Краснодар 8000₽", Decimal(8000)),
        ("Керчь Краснодар 8000 руб", Decimal(8000)),
        ("Керчь Краснодар 8000р на руки", Decimal(8000)),
    ],
)
def test_extract_price_from_common_phrasing(text, expected):
    assert extract_price(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "",
        "Курск Тольятти сейчас, 1 пассажир",
        "Курск Тольятти +7 900 123 45 67",  # телефон — не цена
        "Курск Тольятти 89001234567",
        "Курск Тольятти в 18:30",
        "Курск Тольятти 3000 водителю или 5000 водителю",  # две разные суммы — не гадаем
    ],
)
def test_extract_price_refuses_to_guess(text):
    assert extract_price(text) is None


def test_fill_missing_prices_uses_whole_text_for_single_order():
    order = ParsedOrder(from_city="Курск", to_city="Тольятти")
    fill_missing_prices([order], "Курск\nТольятти\n30000 водителю")
    assert order.client_price == Decimal(30000)


def test_fill_missing_prices_does_not_override_model():
    order = ParsedOrder(from_city="Курск", to_city="Тольятти", client_price=Decimal(25000))
    fill_missing_prices([order], "Курск Тольятти 30000 водителю")
    assert order.client_price == Decimal(25000)


def test_fill_missing_prices_in_batch_uses_each_snippet():
    first = ParsedOrder(from_city="А", to_city="Б", raw_snippet="А - Б 5000 водителю")
    second = ParsedOrder(from_city="В", to_city="Г", raw_snippet="В - Г")
    fill_missing_prices([first, second], "А - Б 5000 водителю\n\nВ - Г")
    assert first.client_price == Decimal(5000)
    assert second.client_price is None  # в пачке цену чужой заявки не присваиваем
