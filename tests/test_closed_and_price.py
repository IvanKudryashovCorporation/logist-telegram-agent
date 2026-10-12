import pytest

from app.parsing.closed import has_closed_lock, has_closed_mark
from app.parsing.price import has_price_request


@pytest.mark.parametrize(
    "text",
    [
        "12.10 в 15:00\nСимферополь - Керчь\n4 чел. 4 чемодана. Комфорт.\nОтдан ✅",
        "Симферополь - Ялта 3000\nЗабрали",
        "Симферополь - Ялта 3000\n✅ взяли",
        "Заказ отдан ✅ Симферополь - Ялта",
        "Симферополь - Ялта 3000\nНеактуально!",
        "Симферополь - Ялта 3000\nЗакрыто.",
    ],
)
def test_words_close_an_order(text):
    assert has_closed_mark(text) and has_closed_lock(text)


@pytest.mark.parametrize(
    "text",
    [
        "Симферополь - Ялта 3000, отдан на руки водителю в аэропорту",
        "Симферополь - Ялта 3000. Заказ актуален, ✅ Взять",
        "Симферополь - Ялта 3000, пассажир занят до обеда, будет в 15:00",
        "",
        None,
    ],
)
def test_ordinary_phrases_do_not_close_an_order(text):
    assert not has_closed_mark(text)


@pytest.mark.parametrize(
    "text",
    ["Симферополь - Керчь, запрос цены", "цена по запросу", "Цена договорная", "предлагайте свою цену", "Торг уместен",
     "Ваша цена?", "сколько возьмёте", "Зея - Благовещенск по цене сами 10%", "по вашей цене"],
)
def test_price_request_phrases(text):
    assert has_price_request(text)


@pytest.mark.parametrize("text", ["Симферополь - Керчь 4 чел.", "", None])
def test_no_price_request_in_plain_order(text):
    assert not has_price_request(text)
