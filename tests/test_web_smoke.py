"""Сквозные HTTP-проверки витрины: страницы, действия водителя, служебные маршруты.

Это «дымовые» тесты — они проходят весь стек (роутер → запросы → шаблоны),
поэтому ловят то, что не видно в модульных тестах: отсутствующую переменную
в шаблоне, неверный код ответа, сломанную cookie.
"""

import re
from pathlib import Path
from urllib.parse import unquote

import pytest
from sqlalchemy import select

from app.models import Order, OrderStatus
from app.web import queries

TEMPLATES = Path(__file__).resolve().parent.parent / "app" / "web" / "templates"

TOKEN_A = "smoke-driver-a"
TOKEN_B = "smoke-driver-b"
PHONE = "+79990000000"
MASKED_PHONE = "***-***-**00"


def _as_driver(client, token: str) -> None:
    """Переключает «текущего водителя» — сайт различает их только по cookie."""
    client.cookies.set("driver_id", token)
    client.cookies.delete("flash_notice")


async def _reload(session, order_id: int) -> Order:
    """Перечитывает заказ из БД.

    Роуты работают в своей сессии, а фикстура ``session`` кэширует объекты в
    identity map: без сброса кэша тест увидел бы состояние «до» действия.
    """
    session.expire_all()
    return (await session.execute(select(Order).where(Order.id == order_id))).scalar_one()


# --- Лента -------------------------------------------------------------------


async def test_feed_renders_and_issues_driver_cookie(client, make_order):
    order = await make_order(from_city="Симферополь", to_city="Сочи", price="14000")

    response = await client.get("/")

    assert response.status_code == 200
    assert "Симферополь" in response.text
    assert f'data-order-id="{order.id}"' in response.text  # строка заказа в списке
    # Без cookie водителя сайт не может хранить «мои заказы» — выдаёт её сразу.
    assert response.cookies.get("driver_id")


async def test_feed_pagination_links_keep_filters(client, make_order):
    """Ссылки на страницы обязаны сохранять фильтры, поиск и сортировку."""
    for index in range(12):
        await make_order(from_city="Симферополь", to_city="Сочи", price=str(1000 + index * 100))

    response = await client.get("/?from_city=Симферополь&sort=price&page=2")

    assert response.status_code == 200
    assert "page=1" in response.text or "page=3" in response.text
    assert "from_city=" in response.text
    assert "sort=price" in response.text
    # WEB_PAGE_SIZE=5 в тестах, 12 заказов → страница 2 существует, страница 3 — последняя.
    assert "page=3" in response.text


async def test_feed_shows_result_count(client, make_order):
    for _ in range(3):
        await make_order()

    response = await client.get("/")

    assert response.status_code == 200
    assert "3" in response.text


async def test_my_orders_page(client, make_order):
    order = await make_order()
    _as_driver(client, TOKEN_A)
    await client.post(f"/orders/{order.id}/take")

    response = await client.get("/my")

    assert response.status_code == 200
    assert f'data-order-id="{order.id}"' in response.text


async def test_empty_feed_renders_without_error(client):
    response = await client.get("/")

    assert response.status_code == 200


# --- Карточка заказа и маскирование контактов --------------------------------


async def test_missing_order_returns_404_for_admin(client, monkeypatch):
    monkeypatch.setattr("app.web.routes.is_admin", lambda request: True)

    response = await client.get("/orders/999999")

    assert response.status_code == 404


RAW_TEXT = "24.05 18:00 Симферополь — Сочи, 2 пассажира, 14000, Иван Петров +79990000000"


async def test_unknown_page_returns_404(client):
    response = await client.get("/такой-страницы-нет")

    assert response.status_code == 404


# --- Действия водителя через HTTP --------------------------------------------


async def test_take_action_redirects_and_persists(client, make_order, session):
    order = await make_order()
    _as_driver(client, TOKEN_A)

    response = await client.post(f"/orders/{order.id}/take")

    assert response.status_code == 303
    assert response.headers["location"] == "/"  # карточки заказа нет — возвращаем в ленту
    assert (await _reload(session, order.id)).taken_by_token == TOKEN_A


async def test_second_driver_sees_explanation_notice(client, make_order):
    """Регрессия на UnicodeEncodeError: русское сообщение в Set-Cookie.

    Заголовок HTTP кодируется в latin-1, поэтому текст уведомления обязан
    URL-кодироваться. Без этого любое действие с сообщением падало в 500.
    """
    order = await make_order()
    _as_driver(client, TOKEN_A)
    await client.post(f"/orders/{order.id}/take")

    _as_driver(client, TOKEN_B)
    response = await client.post(f"/orders/{order.id}/take")
    assert response.status_code == 303

    page = await client.get("/")
    assert page.status_code == 200
    assert "другой водитель" in page.text


async def test_repeat_take_does_not_show_false_error(client, make_order):
    order = await make_order()
    _as_driver(client, TOKEN_A)
    await client.post(f"/orders/{order.id}/take")

    response = await client.post(f"/orders/{order.id}/take")
    assert response.status_code == 303

    page = await client.get("/")
    assert "уже закрыт" not in page.text


async def test_agree_and_release_through_http(client, make_order, session):
    order = await make_order()
    _as_driver(client, TOKEN_A)
    await client.post(f"/orders/{order.id}/take")

    agreed = await client.post(f"/orders/{order.id}/agree")
    assert agreed.status_code == 303
    assert (await _reload(session, order.id)).status == OrderStatus.AGREED

    released = await client.post(f"/orders/{order.id}/release")
    assert released.status_code == 303
    assert released.headers["location"] == "/my"
    reloaded = await _reload(session, order.id)
    assert reloaded.taken_by_token is None
    assert reloaded.status == OrderStatus.NEW


async def test_contact_redirects_to_dispatcher_without_taking_order(client, make_order, session):
    order = await make_order(dispatcher_username="super_dispatcher")
    _as_driver(client, TOKEN_A)

    response = await client.post(f"/orders/{order.id}/contact")

    assert response.status_code == 303
    location = response.headers["location"]
    assert location.startswith("https://t.me/super_dispatcher?text=")
    message = unquote(location.split("?text=", 1)[1])
    assert message.startswith("Здравствуйте! Заказ Симферополь → Сочи")
    assert message.endswith("— актуально?")
    # Контакты клиента в чат с диспетчером не утекают.
    assert "+79990000000" not in message

    # Главное: переход в чат заказ не берёт — он остаётся в общей ленте.
    reloaded = await _reload(session, order.id)
    assert reloaded.taken_by_token is None
    assert reloaded.status == OrderStatus.NEW
    assert order.id in {o.id for o in (await queries.fetch_feed(session, page_size=50)).items}


async def test_contact_is_refused_for_order_taken_by_other(client, make_order):
    order = await make_order(taken_by_token=TOKEN_B)
    _as_driver(client, TOKEN_A)

    response = await client.post(f"/orders/{order.id}/contact")

    assert response.status_code == 303
    assert response.headers["location"] == "/"


async def test_agree_takes_free_order_and_moves_it_to_my_orders(client, make_order, session):
    order = await make_order()
    _as_driver(client, TOKEN_A)

    response = await client.post(f"/orders/{order.id}/agree")

    assert response.status_code == 303
    assert response.headers["location"] == "/my"
    reloaded = await _reload(session, order.id)
    assert reloaded.taken_by_token == TOKEN_A
    assert reloaded.status == OrderStatus.AGREED
    assert order.id not in {o.id for o in (await queries.fetch_feed(session, page_size=50)).items}
    assert f'data-order-id="{order.id}"' in (await client.get("/my")).text


async def test_second_driver_cannot_agree_taken_order(client, make_order, session):
    order = await make_order()
    _as_driver(client, TOKEN_A)
    await client.post(f"/orders/{order.id}/agree")

    _as_driver(client, TOKEN_B)
    response = await client.post(f"/orders/{order.id}/agree")

    assert response.status_code == 303
    assert response.headers["location"] == "/"
    assert (await _reload(session, order.id)).taken_by_token == TOKEN_A


async def test_feedback_marks_problem_and_thanks(client, make_order, session):
    order = await make_order()
    _as_driver(client, TOKEN_A)
    await client.post(f"/orders/{order.id}/take")

    response = await client.post(
        f"/orders/{order.id}/feedback",
        data={"reason": "price_outdated", "note": "диспетчер просил дороже"},
    )
    assert response.status_code == 303

    reloaded = await _reload(session, order.id)
    assert reloaded.has_problem is True
    assert "Цена неактуальна" in reloaded.problem_note
    assert "диспетчер просил дороже" in reloaded.problem_note

    assert "Спасибо" in (await client.get("/")).text


async def test_feedback_without_reason_is_rejected(client, make_order, session):
    order = await make_order()
    _as_driver(client, TOKEN_A)
    await client.post(f"/orders/{order.id}/take")

    response = await client.post(f"/orders/{order.id}/feedback", data={"reason": "", "note": ""})
    assert response.status_code == 303

    assert (await _reload(session, order.id)).has_problem is False


async def test_feedback_from_stranger_is_rejected(client, make_order, session):
    order = await make_order()
    _as_driver(client, TOKEN_A)
    await client.post(f"/orders/{order.id}/take")

    _as_driver(client, TOKEN_B)
    await client.post(f"/orders/{order.id}/feedback", data={"reason": "no_answer"})

    assert (await _reload(session, order.id)).has_problem is False


async def test_actions_on_missing_order_do_not_crash(client):
    for path in ("take", "release", "agree", "contact", "complete"):
        response = await client.post(f"/orders/999999/{path}")
        assert response.status_code in (303, 404), path


# --- Служебные маршруты и заголовки ------------------------------------------


async def test_healthz_reports_database_ok(client):
    response = await client.get("/healthz")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["database"] is True
    # О существовании админки healthz не сообщает: для чужих её нет.
    assert "admin_enabled" not in payload


async def test_robots_txt_forbids_indexing_except_public_pages(client):
    response = await client.get("/robots.txt")

    assert response.status_code == 200
    assert "Disallow: /" in response.text


@pytest.mark.parametrize(
    "status", [OrderStatus.EXPIRED, OrderStatus.CANCELLED, OrderStatus.AGREED]
)
async def test_closed_order_page_is_open_for_admin(client, make_order, monkeypatch, status):
    monkeypatch.setattr("app.web.routes.is_admin", lambda request: True)
    order = await make_order(status=status)
    _as_driver(client, TOKEN_B)

    response = await client.get(f"/orders/{order.id}")

    assert response.status_code == 200
    assert "Режим админа" in response.text
    assert "больше не доступен" not in response.text
    assert "Договорился с диспетчером" not in response.text


async def test_closed_order_stays_visible_in_owners_history(client, make_order):
    order = await make_order(status=OrderStatus.EXPIRED, taken_by_token=TOKEN_A)
    _as_driver(client, TOKEN_A)

    response = await client.get("/my")

    assert response.status_code == 200
    assert f'data-order-id="{order.id}"' in response.text


async def test_complete_button_flow_over_http(client, make_order, session):
    order = await make_order()
    _as_driver(client, TOKEN_A)

    before = (await client.get("/my")).text
    assert "Выполнил заказ" not in before  # пока не «Договорился» — кнопки нет

    await client.post(f"/orders/{order.id}/agree")
    agreed_page = (await client.get("/my")).text
    assert "Выполнил заказ" in agreed_page and "Отменить" in agreed_page

    response = await client.post(f"/orders/{order.id}/complete")
    assert response.status_code == 303 and response.headers["location"] == "/my"
    assert (await _reload(session, order.id)).status == OrderStatus.COMPLETED

    done_page = (await client.get("/my")).text
    assert "Выполнил заказ" not in done_page and "Отменить" not in done_page
    assert "Выполнен" in done_page


async def test_in_progress_order_still_has_complete_and_cancel(client, make_order):
    await make_order(status=OrderStatus.IN_PROGRESS, taken_by_token=TOKEN_A)
    _as_driver(client, TOKEN_A)

    page = (await client.get("/my")).text

    assert "Выполнил заказ" in page and "Отменить" in page and "В работе" in page


async def test_complete_for_stranger_is_refused(client, make_order, session):
    order = await make_order(status=OrderStatus.AGREED, taken_by_token=TOKEN_A)
    _as_driver(client, TOKEN_B)

    response = await client.post(f"/orders/{order.id}/complete")

    assert response.status_code == 303
    assert (await _reload(session, order.id)).status == OrderStatus.AGREED


async def test_feed_has_theme_toggle_and_no_refresh_button(client, make_order):
    await make_order()

    page = (await client.get("/")).text

    assert 'id="themeToggle"' in page
    assert 'id="refreshBtn"' not in page
    # Тема выбирается до отрисовки, иначе при тёмной теме мигает белый экран.
    assert page.index("/static/theme.js") < page.index("/static/style.css")
    assert "defer" not in page.split("/static/theme.js")[1].split(">")[0]
    assert (await client.get("/static/theme.js")).status_code == 200


async def test_static_urls_carry_a_version_so_browsers_drop_stale_files(client):
    page = (await client.get("/")).text

    for name in ("style.css", "app.js", "theme.js"):
        assert re.search(rf"/static/{re.escape(name)}\?v=\d+", page), name


async def test_brand_icons_are_linked_and_served(client):
    page = (await client.get("/")).text

    assert 'rel="icon"' in page and "favicon.png" in page
    assert 'rel="apple-touch-icon"' in page
    assert 'class="logo"' in page and "logo.png" in page

    for path in ("/static/logo.png", "/static/favicon.png", "/static/apple-touch-icon.png", "/favicon.ico"):
        response = await client.get(path)
        assert response.status_code == 200, path
        assert len(response.content) > 500, path


async def test_header_brand_links_to_the_feed_from_any_page(client, make_order):
    await make_order()

    for path in ("/", "/favorites"):
        page = (await client.get(path)).text
        assert re.search(r'<a href="/" class="brand"[^>]*>\s*<img class="logo"', page), path
        assert "Лента заказов</span>" in page, path


# --- Список, сердечко, избранное, карточки заказа нет ---------------------------------------


async def test_order_card_is_closed_for_drivers(client, make_order):
    order = await make_order()
    _as_driver(client, TOKEN_A)

    response = await client.get(f"/orders/{order.id}?back=from_city%3D%D0%A1%D0%BE%D1%87%D0%B8")

    assert response.status_code == 303
    assert unquote(response.headers["location"]) == "/?from_city=Сочи"


async def test_closed_order_card_redirects_strangers_and_stays_open_for_admin(client, make_order, monkeypatch):
    order = await make_order(status=OrderStatus.EXPIRED)
    _as_driver(client, TOKEN_B)

    assert (await client.get(f"/orders/{order.id}")).status_code == 303
    monkeypatch.setattr("app.web.routes.is_admin", lambda request: True)
    assert (await client.get(f"/orders/{order.id}")).status_code == 200


async def test_feed_is_a_list_with_heart_and_message_buttons(client, make_order):
    order = await make_order(client_phone=PHONE, client_name="Иван Петров")

    page = (await client.get("/")).text

    assert 'class="order-list"' in page and 'class="order-row' in page
    assert f"/orders/{order.id}/contact" in page and "fav-btn" in page
    assert "Подробнее" not in page  # кнопки «Подробнее» и ссылок на карточку нет
    assert f'href="/orders/{order.id}' not in page
    assert PHONE not in page and "Иван Петров" not in page


async def test_feed_has_no_sorting_and_tabs_have_favorites_and_filters(client, make_order):
    await make_order()

    page = (await client.get("/?sort=price&dir=asc")).text

    assert "sortSelect" not in page and "Сортировать" not in page
    assert ">Избранное" in page and "Мои заказы" not in page
    assert 'id="filtersDetails"' in page and "Фильтры" in page
    assert page.index("Избранное") < page.index('id="filtersDetails"')  # кнопка фильтров — во вкладках


async def test_radius_is_a_slider(client):
    page = (await client.get("/?from_city=Сочи&from_radius=130")).text

    assert 'type="range"' in page and 'name="from_radius"' in page and 'value="130"' in page
    assert "radius-select" not in page.split('id="filtersForm"', 1)[1].split("Тип авто", 1)[0]


async def test_heart_adds_and_removes_favorite(client, make_order, session):
    order = await make_order()
    _as_driver(client, TOKEN_A)
    headers = {"accept": "application/json"}

    added = await client.post(f"/orders/{order.id}/favorite", headers=headers)
    assert added.json() == {"favorite": True}
    assert 'fav-btn on' in (await client.get("/")).text
    assert f'data-order-id="{order.id}"' in (await client.get("/favorites")).text
    # Закладка не прячет заказ от других и не «берёт» его.
    assert (await _reload(session, order.id)).taken_by_token is None
    _as_driver(client, TOKEN_B)
    assert 'fav-btn on' not in (await client.get("/")).text

    _as_driver(client, TOKEN_A)
    removed = await client.post(f"/orders/{order.id}/favorite", headers=headers)
    assert removed.json() == {"favorite": False}
    assert f'data-order-id="{order.id}"' not in (await client.get("/favorites")).text


async def test_favorite_for_missing_order_is_404(client):
    response = await client.post("/orders/999999/favorite", headers={"accept": "application/json"})

    assert response.status_code == 404


async def test_taken_order_in_favorites_shows_client_contacts_to_owner_only(client, make_order):
    order = await make_order(client_phone=PHONE, client_name="Иван Петров")
    _as_driver(client, TOKEN_A)
    await client.post(f"/orders/{order.id}/agree")

    assert PHONE in (await client.get("/favorites")).text

    _as_driver(client, TOKEN_B)
    assert PHONE not in (await client.get("/favorites")).text
