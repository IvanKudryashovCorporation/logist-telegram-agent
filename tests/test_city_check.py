"""Модель не должна подменять город, которого нет в тексте заявки («валнаваха» → «Волгоград»)."""

import pytest

from app.parsing import llm_parser
from app.parsing.city_check import city_is_supported, unsupported_cities
from app.parsing.llm_parser import ParseResult
from app.parsing.schema import ParsedOrder

TEXT = "Завтра 6 утра валнаваха таганрог 5000"


def test_invented_city_is_not_supported_but_real_ones_are():
    assert not city_is_supported("Волгоград", TEXT)
    assert city_is_supported("Таганрог", TEXT)
    assert unsupported_cities(["Волгоград", "Таганрог"], TEXT) == ["Волгоград"]


@pytest.mark.parametrize("city, text", [
    ("Симферополь", "Симф - Ялта 3000 завтра"),  # сокращение из справочника
    ("Ростов-на-Дону", "Ростов ЖД - Донецк 8000"),  # начало названия
    ("Белоглинка (Симферополь)", "Белоглинка → Ялта 2500"),  # пояснение в скобках
    ("Волноваха", "из Волновахи в Таганрог 5000"),  # падеж
    ("Москва", "Внуково → Тула 4000"),  # аэропорт вместо города: проверка не применяется
    ("Москва", "метро Войковская → Волгоград 30000"),
])
def test_legit_cities_are_supported(city, text):
    assert unsupported_cities([city], text) == []


def _order(**kwargs):
    return ParsedOrder(client_price=5000, **kwargs)


async def test_unsupported_city_is_checked_by_second_provider(monkeypatch):
    primary = ParseResult(orders=[_order(from_city="Волгоград", to_city="Таганрог")], model="primary")
    second = ParseResult(orders=[_order(from_city="Волноваха", to_city="Таганрог")], model="second")
    monkeypatch.setattr(llm_parser.settings, "llm_model", "primary")
    monkeypatch.setattr(llm_parser, "_fallback_client", object())
    calls = []

    async def fake_parse_with(text, today, fallback, priority=False):
        calls.append(fallback)
        return second

    monkeypatch.setattr(llm_parser, "_parse_with", fake_parse_with)

    result = await llm_parser._verify_cities(primary, "Завтра 6 утра волноваха таганрог 5000", "2026-10-20")

    assert calls == [True] and result.orders[0].from_city == "Волноваха"


async def test_city_is_erased_if_nobody_confirms_it(monkeypatch):
    primary = ParseResult(orders=[_order(from_city="Волгоград", to_city="Таганрог")], model="primary")
    monkeypatch.setattr(llm_parser.settings, "llm_model", "primary")
    monkeypatch.setattr(llm_parser, "_fallback_client", object())

    async def same_again(text, today, fallback, priority=False):
        return ParseResult(orders=[_order(from_city="Волгоград", to_city="Таганрог")], model="second")

    monkeypatch.setattr(llm_parser, "_parse_with", same_again)

    result = await llm_parser._verify_cities(primary, TEXT, "2026-10-20")

    assert result.orders[0].from_city is None and result.orders[0].to_city == "Таганрог"


async def test_confirmed_cities_cost_no_extra_call(monkeypatch):
    primary = ParseResult(orders=[_order(from_city="Ростов-на-Дону", to_city="Донецк")], model="primary")

    async def must_not_be_called(*args, **kwargs):
        raise AssertionError("второй провайдер не нужен")

    monkeypatch.setattr(llm_parser, "_parse_with", must_not_be_called)

    result = await llm_parser._verify_cities(primary, "Ростов ЖД - Донецк 8000", "2026-10-20")

    assert result is primary
