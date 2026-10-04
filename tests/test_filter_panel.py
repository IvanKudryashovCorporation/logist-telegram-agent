"""Панель фильтра: «Радиус», живой счётчик «Применить (N)», кнопка внизу, закрытие."""

import re
from pathlib import Path

from app.web import queries
from app.web.filters import Filters

STATIC = Path(__file__).resolve().parent.parent / "app" / "web" / "static"


async def _seed(make_order):
    await make_order(from_city="Сочи", to_city="Краснодар", price="9000", passengers=2)
    await make_order(from_city="Сочи", to_city="Адлер", price="3000", passengers=2)
    await make_order(from_city="Севастополь", to_city="Сочи", price="12000", passengers=6)
    await make_order(from_city="Симферополь", to_city="Ялта", price="5000", passengers=2)


# --- «Радиус» вместо «Только город» ----------------------------------------------------


async def test_radius_select_says_radius(client, make_order):
    await _seed(make_order)

    page = await client.get("/")

    assert "Только город" not in page.text
    assert '<option value="0"' in page.text and ">Радиус</option>" in page.text
    assert "+ 50 км" in page.text


# --- Счётчик ---------------------------------------------------------------------------


async def test_feed_count_matches_feed_total(client, make_order, session):
    await _seed(make_order)

    cases = [
        {},
        {"from_city": "Сочи"},
        {"vehicle": "minivan"},
        {"vehicle": "car", "price_min": "4000"},
        {"price_max": "5000"},
        {"q": "сочи"},
        {"from_city": "Сочи", "from_radius": "50"},
        {"from_city": "Нигдеград"},
    ]
    for params in cases:
        response = await client.get("/feed/count", params=params)
        assert response.status_code == 200, params
        assert response.headers["cache-control"] == "no-store"

        q = params.get("q", "")
        filters = Filters(**{k: v for k, v in params.items() if k != "q"})
        expected = (await queries.fetch_feed(session, filters=filters, q=q, page_size=200)).total
        assert response.json() == {"total": expected}, params


async def test_feed_count_ignores_garbage(client, make_order):
    await _seed(make_order)

    response = await client.get(
        "/feed/count",
        params={"price_min": "много", "date_from": "вчера", "from_radius": "7", "vehicle": "танк"},
    )

    assert response.status_code == 200
    assert response.json() == {"total": 4}


async def test_apply_button_shows_the_current_total(client, make_order):
    await _seed(make_order)

    page = await client.get("/?from_city=Сочи")

    shown = re.search(r"Показано \d+ из (\d+)", page.text).group(1)
    button = re.search(r'id="filterApply">Применить <span id="filterApplyCount">\((\d+)\)</span>', page.text)
    assert button and button.group(1) == shown == "2"


# --- Панель закрыта после применения ------------------------------------------------------


async def test_filter_panel_is_closed_even_when_a_filter_is_active(client, make_order):
    await _seed(make_order)

    page = await client.get("/?from_city=Сочи&vehicle=car")

    details = re.search(r"<details[^>]*filters-details[^>]*>", page.text).group(0)
    assert " open" not in details
    assert 'class="tab-count"' in page.text  # активный фильтр виден по счётчику у «Фильтры»


# --- Фронтенд-регрессии --------------------------------------------------------------------


def test_script_recounts_closes_and_notifies_on_hidden_fields():
    js = (STATIC / "app.js").read_text(encoding="utf-8")

    assert "/feed/count" in js  # живой счётчик
    assert "details.open = false" in js  # закрытие после «Применить»
    assert js.count("dispatchEvent(new Event('change'") >= 2  # города и даты


def test_apply_button_sticks_to_the_bottom_of_the_panel():
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    block = re.search(r"\.filters-actions \{.*?\}", css, re.S).group(0)

    assert "position: sticky" in block and "bottom:" in block
    panel = re.search(r"\.filters-panel \{.*?\n    \}", css, re.S).group(0)
    assert "overflow-y: auto" in panel and "max-height" in panel
