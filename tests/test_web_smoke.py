"""Сквозные HTTP-проверки витрины: страницы, действия водителя, служебные маршруты.

Это «дымовые» тесты — они проходят весь стек (роутер → запросы → шаблоны),
поэтому ловят то, что не видно в модульных тестах: отсутствующую переменную
в шаблоне, неверный код ответа, сломанную cookie.
"""

import re
from pathlib import Path

import pytest
from sqlalchemy import select

from app.models import Order, OrderStatus

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
    assert f"/orders/{order.id}/take-and-contact?back={back_param}" in detail_response.text


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


async def test_take_and_contact_redirects_to_dispatcher(client, make_order):
    order = await make_order(dispatcher_username="super_dispatcher")
    _as_driver(client, TOKEN_A)

    response = await client.post(f"/orders/{order.id}/take-and-contact")

    assert response.status_code == 303
    assert response.headers["location"] == "https://t.me/super_dispatcher"


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
    for path in ("take", "release", "agree", "take-and-contact"):
        response = await client.post(f"/orders/999999/{path}")
        assert response.status_code in (303, 404), path


# --- Служебные маршруты и заголовки ------------------------------------------


async def test_healthz_reports_database_ok(client):
    response = await client.get("/healthz")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["database"] is True
    assert payload["admin_enabled"] is True


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

    assert "/static/style.css" in base
    assert "/static/app.js" in base
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
