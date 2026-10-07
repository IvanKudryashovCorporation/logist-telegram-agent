"""Предфильтр сообщений перед отправкой в LLM.

Экономит деньги и снимает риск упереться в rate-limit провайдера: в рабочих
чатах большинство сообщений — переписка, а не заявки. Фильтр намеренно
консервативен, поэтому главное требование к тестам — проверять не только
«болтовня отсекается», но и «настоящая заявка НЕ отсекается»: потерянная
заявка стоит намного дороже лишнего вызова модели.
"""

import pytest

from app.parsing.prefilter import (
    PrefilterDecision,
    mentions_known_city,
    normalize,
    prefilter,
)

CHATTER = [
    "ок",
    "ОК",
    "ok",
    "+",
    "+++",
    "принял",
    "спасибо",
    "добрый день",
    "свободен",
    "не актуально",
    "договорились",
    "в пути",
    "выехал",
    "ок!!!",
    "  принял  ",
]

ORDERS = [
    # Полноценная заявка — с телефоном, ценой, временем и маршрутом.
    "24.05 18:00 Симферополь — Сочи, 2 пассажира, 14000, Иван +79990000000",
    # Без телефона, но с ценой и городами.
    "Завтра Симферополь - Сочи 15000, выезд рано утром",
    # Совсем короткая, но с цифрами и стрелкой маршрута.
    "Керчь→Краснодар 8000",
    # Только город и маркер встречи — цифр нет.
    "встретить в аэропорту Симферополь вечером",
    # Телефон в «грязном» формате.
    "Перевозка 8(978)123-45-67 маршрут Севастополь Москва",
]


@pytest.mark.parametrize("text", CHATTER)
def test_chatter_is_rejected(text):
    decision = prefilter(text)

    assert decision.send_to_llm is False
    assert decision.reason, "причина отказа обязательна — по ней ищут ложные срабатывания"


@pytest.mark.parametrize("text", ORDERS)
def test_real_orders_are_never_rejected(text):
    decision = prefilter(text)

    assert decision.send_to_llm is True, f"заявка не должна отсекаться: {text!r} ({decision.reason})"


@pytest.mark.parametrize(
    ("text", "expected_reason"),
    [
        ("", "empty"),
        (None, "empty"),
        ("   ", "empty"),
        # Две буквы — отсекается ещё до проверки на «болтовню».
        ("ок", "too_short"),
        ("++", "too_short"),
        # А вот с пунктуацией это уже полноценное совпадение со списком.
        ("ок!!!", "chatter"),
        ("принял", "chatter"),
        ("договорились", "chatter"),
        ("1234567", "no_letters"),
        ("кто где", "too_short_no_signal"),
        ("Всем хорошего настроения", "no_signal"),
    ],
)
def test_rejection_reasons(text, expected_reason):
    assert prefilter(text).reason == expected_reason


def test_short_text_without_digits_is_rejected():
    """Короткая реплика без единой цифры — переписка, а не заявка."""
    decision = prefilter("кто где")

    assert decision.send_to_llm is False
    assert decision.reason == "too_short_no_signal"


def test_digits_are_enough_signal():
    """Цена или время почти всегда означают заявку — пропускаем даже короткий текст."""
    assert prefilter("абвгд 1500").reason == "has_digits"
    assert prefilter("абвгд 1500").send_to_llm is True


def test_pure_number_is_rejected():
    """Одни цифры без букв — это не заявка (номер, код, «+» в чате)."""
    decision = prefilter("1500")

    assert decision.send_to_llm is False
    assert decision.reason == "no_letters"


def test_decision_is_truthy_when_sending():
    assert bool(PrefilterDecision(True, "has_phone")) is True
    assert bool(PrefilterDecision(False, "chatter")) is False


def test_normalize_collapses_whitespace_and_case():
    assert normalize("  ПРИВЕТ   МИР \n\t ") == "привет мир"
    assert normalize(None) == ""


def test_mentions_known_city():
    assert mentions_known_city("едем в симферополь") is True
    assert mentions_known_city("просто текст без географии") is False
    assert mentions_known_city("") is False


def test_empty_string_is_falsy_not_error():
    """Пустое/None сообщение не должно ронять обработчик событий."""
    assert prefilter(None).send_to_llm is False
    assert prefilter("").send_to_llm is False
