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
    assert f"/orders/{order.id}" in response.text
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


async def test_order_detail_back_link_preserves_feed_filters(client, make_order):
    """Уходя с карточки заказа, водитель должен вернуться к тем же фильтрам
    и сортировке, что были в ленте — а не на дашборд с настройками по
    умолчанию."""
    order = await make_order(from_city="Сочи", to_city="Адлер")

    feed_response = await client.get("/?from_city=Сочи&sort=price")
    assert f"/orders/{order.id}?back=" in feed_response.text

    match = re.search(rf"/orders/{order.id}\?back=([^\"&]+)", feed_response.text)
    assert match is not None, "ссылка на карточку не сохраняет текущий запрос"
    back_param = match.group(1)

    detail_response = await client.get(f"/orders/{order.id}?back={back_param}")
    assert detail_response.status_code == 200
    # Ссылка "← ко всем заказам" обязана вернуть на ту же ленту, а не на "/".
    back_link_match = re.search(r'href="(/\?[^"]+)" class="back-link"', detail_response.text)
    assert back_link_match is not None, "ссылка назад не сохранила текущий запрос"
    back_href = back_link_match.group(1)
    assert "sort=price" in back_href
    assert "from_city=" in back_href
    # Кнопка действия тоже должна нести back — иначе после неё это потеряется.
    assert f"/orders/{order.id}/contact?back={back_param}" in detail_response.text


async def test_order_detail_back_link_is_plain_without_query(client, make_order):
    """Без фильтров в ленте ссылка назад — просто "/", без пустого "?"."""
    order = await make_order()

    response = await client.get(f"/orders/{order.id}")

    assert response.status_code == 200
    assert 'href="/" class="back-link"' in response.text


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
    assert f"/orders/{order.id}" in response.text


async def test_empty_feed_renders_without_error(client):
    response = await client.get("/")

    assert response.status_code == 200


# --- Карточка заказа и маскирование контактов --------------------------------


async def test_order_detail_masks_contacts_for_stranger(client, make_order):
    order = await make_order(client_phone=PHONE, client_name="Иван Петров")

    response = await client.get(f"/orders/{order.id}")

    assert response.status_code == 200
    assert PHONE not in response.text, "полный телефон не должен быть виден посторонним"
    assert MASKED_PHONE in response.text
    assert "Иван Петров" not in response.text


async def test_order_detail_shows_full_contacts_to_owner(client, make_order):
    order = await make_order(client_phone=PHONE, client_name="Иван Петров")
    _as_driver(client, TOKEN_A)
    await client.post(f"/orders/{order.id}/take")

    response = await client.get(f"/orders/{order.id}")

    assert response.status_code == 200
    assert PHONE in response.text
    assert "Иван Петров" in response.text


async def test_masking_can_be_switched_off(client, make_order, monkeypatch):
    """MASK_CLIENT_CONTACTS=false — телефон виден всем (например, для закрытой витрины)."""
    from app.config import settings

    monkeypatch.setattr(settings, "mask_client_contacts", False)
    order = await make_order(client_phone=PHONE)

    response = await client.get(f"/orders/{order.id}")

    assert PHONE in response.text


async def test_missing_order_returns_404(client):
    response = await client.get("/orders/999999")

    assert response.status_code == 404


RAW_TEXT = "24.05 18:00 Симферополь — Сочи, 2 пассажира, 14000, Иван Петров +79990000000"


async def test_raw_text_is_redacted_for_stranger(client, make_order):
    """Оригинал заявки не должен выдавать контакты клиента посторонним.

    Без вымарывания masking превращался в декорацию: телефон и имя оставались
    в «исходном тексте заявки», который виден всем.
    """
    order = await make_order(client_phone=PHONE, client_name="Иван Петров", raw_text=RAW_TEXT)

    response = await client.get(f"/orders/{order.id}")
    page = response.text

    assert response.status_code == 200
    assert PHONE not in page
    assert "Петров" not in page
    # Маршрут, время и цена остаются — иначе заявка теряет смысл.
    assert "Симферополь" in page
    assert "14000" in page
    assert "18:00" in page


async def test_raw_text_is_full_for_owner(client, make_order):
    order = await make_order(client_phone=PHONE, client_name="Иван Петров", raw_text=RAW_TEXT)
    _as_driver(client, TOKEN_A)
    await client.post(f"/orders/{order.id}/take")

    page = (await client.get(f"/orders/{order.id}")).text

    assert PHONE in page
    assert "Иван Петров" in page


async def test_unknown_page_returns_404(client):
    response = await client.get("/такой-страницы-нет")

    assert response.status_code == 404


# --- Действия водителя через HTTP --------------------------------------------


async def test_take_action_redirects_and_persists(client, make_order, session):
    order = await make_order()
    _as_driver(client, TOKEN_A)

    response = await client.post(f"/orders/{order.id}/take")

    assert response.status_code == 303
    assert response.headers["location"] == f"/orders/{order.id}"
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

    page = await client.get(f"/orders/{order.id}")
    assert page.status_code == 200
    assert "другой водитель" in page.text


async def test_repeat_take_does_not_show_false_error(client, make_order):
    order = await make_order()
    _as_driver(client, TOKEN_A)
    await client.post(f"/orders/{order.id}/take")

    response = await client.post(f"/orders/{order.id}/take")
    assert response.status_code == 303

    page = await client.get(f"/orders/{order.id}")
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
    assert response.headers["location"] == f"/orders/{order.id}"


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
    assert f"/orders/{order.id}" in (await client.get("/my")).text


async def test_second_driver_cannot_agree_taken_order(client, make_order, session):
    order = await make_order()
    _as_driver(client, TOKEN_A)
    await client.post(f"/orders/{order.id}/agree")

    _as_driver(client, TOKEN_B)
    response = await client.post(f"/orders/{order.id}/agree")

    assert response.status_code == 303
    assert response.headers["location"] == f"/orders/{order.id}"
    assert (await _reload(session, order.id)).taken_by_token == TOKEN_A


async def test_detail_page_has_contact_and_agree_buttons(client, make_order):
    order = await make_order()

    page = (await client.get(f"/orders/{order.id}")).text

    assert "Написать диспетчеру" in page
    assert "Договорился с диспетчером" in page
    assert f"/orders/{order.id}/contact" in page


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

    page = await client.get(f"/orders/{order.id}")
    assert "Спасибо" in page.text


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


async def test_robots_txt_forbids_indexing(client):
    response = await client.get("/robots.txt")

    assert response.status_code == 200
    assert "Disallow: /" in response.text


@pytest.mark.parametrize(
    ("path", "marker"),
    [
        ("/static/style.css", ".card"),
        ("/static/app.js", "addEventListener"),
        ("/static/admin.css", ".admin-nav"),
    ],
)
async def test_static_assets_are_served(client, path, marker):
    response = await client.get(path)

    assert response.status_code == 200
    assert marker in response.text
    assert response.headers["content-type"].startswith(("text/css", "application/javascript", "text/javascript"))


async def test_templates_have_no_large_inline_assets(client):
    """CSS/JS вынесены в /static: иначе их нельзя кэшировать, а HTML раздут."""
    base = (TEMPLATES / "base.html").read_text(encoding="utf-8")

    assert "static_url('style.css')" in base
    assert "static_url('app.js')" in base
    assert "<style>" not in base, "инлайн-стили должны жить в static/style.css"
    assert "<script>" not in base.replace("<script src=", ""), (
        "инлайн-скрипты должны жить в static/app.js"
    )


async def test_security_headers_present(client):
    response = await client.get("/")

    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "same-origin"


async def test_hsts_absent_on_localhost(client):
    """HSTS по http://localhost сломал бы разработчику доступ к сайту."""
    response = await client.get("/")

    assert "strict-transport-security" not in response.headers


async def test_interactive_docs_are_disabled(client):
    """Публичный /docs перечислил бы все эндпоинты, включая админку."""
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert (await client.get(path)).status_code == 404, path


@pytest.mark.parametrize(
    "status", [OrderStatus.EXPIRED, OrderStatus.CANCELLED, OrderStatus.AGREED]
)
async def test_closed_order_page_is_gone_for_strangers(client, make_order, status):
    order = await make_order(status=status)
    _as_driver(client, TOKEN_B)

    response = await client.get(f"/orders/{order.id}")

    assert response.status_code == 410
    assert "больше не доступен" in response.text
    assert "Написать диспетчеру" not in response.text


async def test_closed_order_stays_visible_in_owners_history(client, make_order):
    order = await make_order(status=OrderStatus.EXPIRED, taken_by_token=TOKEN_A)
    _as_driver(client, TOKEN_A)

    response = await client.get(f"/orders/{order.id}")

    assert response.status_code == 200


async def test_complete_button_flow_over_http(client, make_order, session):
    order = await make_order()
    _as_driver(client, TOKEN_A)

    before = (await client.get(f"/orders/{order.id}")).text
    assert "Выполнил заказ" not in before  # пока не «Договорился» — кнопки нет

    await client.post(f"/orders/{order.id}/agree")
    agreed_page = (await client.get(f"/orders/{order.id}")).text
    assert "Выполнил заказ" in agreed_page and "Отменить" in agreed_page

    response = await client.post(f"/orders/{order.id}/complete")
    assert response.status_code == 303 and response.headers["location"] == "/my"
    assert (await _reload(session, order.id)).status == OrderStatus.COMPLETED

    done_page = (await client.get(f"/orders/{order.id}")).text
    assert "Выполнил заказ" not in done_page and "Отменить" not in done_page
    assert "Выполнен" in done_page


async def test_in_progress_order_still_has_complete_and_cancel(client, make_order):
    order = await make_order(status=OrderStatus.IN_PROGRESS, taken_by_token=TOKEN_A)
    _as_driver(client, TOKEN_A)

    page = (await client.get(f"/orders/{order.id}")).text

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
    order = await make_order()

    for path in ("/", f"/orders/{order.id}"):
        page = (await client.get(path)).text
        assert re.search(r'<a href="/" class="brand"[^>]*>\s*<img class="logo"', page), path
        assert "Лента заказов</span>" in page, path


async def test_app_js_restores_feed_scroll_after_returning_from_an_order(client):
    script = (await client.get("/static/app.js")).text

    assert "scrollBeforeCard" in script and "sessionStorage" in script
    assert "back_forward" in script and "scrollTo(0, saved.y)" in script
    assert "requestAnimationFrame" not in script  # в фоновой вкладке не срабатывает
