"""Админка владельца: доступ, отчёты, ручное управление лентой и очередью.

Админка — единственное место, где видно качество разбора и застрявшие
сообщения, поэтому доступ к ней проверяется особенно придирчиво: сайт
публичный, а здесь и телефоны клиентов, и статистика расходов на LLM.
"""

from datetime import timedelta

import pytest
from sqlalchemy import select

from app.config import settings
from app.db.base import SessionLocal
from app.models import Order, OrderStatus, PendingMessage, PendingStatus
from app.telegram import pending
from app.timeutil import now_utc_naive
from app.web import deps

PASSWORD = "test-admin-password"


async def _login(client, password: str = PASSWORD):
    return await client.post("/admin/login", data={"password": password})


async def _reload_order(session, order_id: int) -> Order:
    session.expire_all()
    return (await session.execute(select(Order).where(Order.id == order_id))).scalar_one()


# --- Доступ ------------------------------------------------------------------


async def test_login_page_is_available(client):
    response = await client.get("/admin/login")

    assert response.status_code == 200
    assert 'type="password"' in response.text


async def test_dashboard_requires_login(client):
    response = await client.get("/admin")

    assert response.status_code == 303
    assert response.headers["location"] == "/admin/login"


@pytest.mark.parametrize("path", ["/admin", "/admin/orders", "/admin/queue"])
async def test_all_sections_are_guarded(client, path):
    assert (await client.get(path)).headers["location"] == "/admin/login"


async def test_wrong_password_is_rejected(client):
    response = await _login(client, "совершенно-не-тот-пароль")

    assert response.status_code == 200
    assert "Неверный пароль" in response.text
    assert "admin_session" not in response.cookies


async def test_correct_password_issues_cookie_and_redirects(client):
    response = await _login(client)

    assert response.status_code == 303
    assert response.headers["location"] == "/admin"
    assert response.cookies.get("admin_session")


async def test_login_page_redirects_authenticated_admin(client):
    await _login(client)

    response = await client.get("/admin/login")

    assert response.status_code == 303
    assert response.headers["location"] == "/admin"


async def test_forged_cookie_is_rejected(client):
    """Подделка сводится к подстановке своей метки времени — подпись не сойдётся."""
    valid = deps.admin_cookie_value()
    expires, _, _ = valid.partition(".")
    forged = f"{int(expires) + 100000}.{valid.partition('.')[2]}"

    client.cookies.set("admin_session", forged)
    response = await client.get("/admin")

    assert response.status_code == 303
    assert response.headers["location"] == "/admin/login"


async def test_expired_cookie_is_rejected(client):
    client.cookies.set("admin_session", deps.admin_cookie_value(ttl=-60))

    assert (await client.get("/admin")).headers["location"] == "/admin/login"


async def test_logout_clears_session(client):
    await _login(client)
    assert (await client.get("/admin")).status_code == 200

    response = await client.post("/admin/logout")

    assert response.status_code == 303
    assert response.headers["location"] == "/"
    assert (await client.get("/admin")).headers["location"] == "/admin/login"


async def test_admin_is_absent_without_password(client, monkeypatch):
    """ADMIN_PASSWORD пуст — админки нет вовсе: 404, а не форма входа."""
    monkeypatch.setattr(settings, "admin_password", "")

    for path in ("/admin", "/admin/login", "/admin/orders", "/admin/queue"):
        assert (await client.get(path)).status_code == 404, path
    assert (await client.post("/admin/login", data={"password": ""})).status_code == 404


# --- Сводка ------------------------------------------------------------------


async def test_dashboard_shows_metrics(client, make_order):
    await _login(client)
    await make_order(price="14000")
    await make_order(status=OrderStatus.CANCELLED, pickup_at=now_utc_naive() + timedelta(days=1))

    response = await client.get("/admin")

    assert response.status_code == 200
    # Сводка по статусам, качество разбора и очередь — то, ради чего админка существует.
    assert "всего заказов" in response.text
    assert "Качество разбора" in response.text
    assert "Очередь повторного разбора" in response.text
    assert "Служебное" in response.text
    assert 'id="problems"' in response.text


async def test_dashboard_lists_problem_orders(client, make_order):
    await _login(client)
    order = await make_order()
    client.cookies.set("driver_id", "admin-problem-driver")
    await client.post(f"/orders/{order.id}/take")
    await client.post(
        f"/orders/{order.id}/feedback", data={"reason": "no_answer", "note": "диспетчер молчит"}
    )

    response = await client.get("/admin")

    assert response.status_code == 200
    assert "диспетчер молчит" in response.text
    assert f"/orders/{order.id}" in response.text


async def test_dashboard_period_is_clamped(client):
    """?days=99999 не должен ни падать, ни вычитывать всю историю."""
    await _login(client)

    for days in (0, -5, 9999):
        assert (await client.get(f"/admin?days={days}")).status_code == 200


# --- Список заказов ----------------------------------------------------------


async def test_orders_list_shows_all_statuses(client, make_order):
    await _login(client)
    live = await make_order(from_city="Симферополь")
    hidden = await make_order(
        from_city="Севастополь", status=OrderStatus.CANCELLED,
        pickup_at=now_utc_naive() + timedelta(days=1),
    )

    response = await client.get("/admin/orders")

    assert response.status_code == 200
    # В отличие от публичной ленты, админ видит и скрытые заявки.
    assert f"/orders/{live.id}" in response.text
    assert f"/orders/{hidden.id}" in response.text


async def test_orders_list_filters_by_status(client, make_order):
    await _login(client)
    live = await make_order()
    hidden = await make_order(
        status=OrderStatus.CANCELLED, pickup_at=now_utc_naive() + timedelta(days=1)
    )

    response = await client.get("/admin/orders?status=cancelled")

    assert response.status_code == 200
    assert f"/orders/{hidden.id}" in response.text
    assert f"/orders/{live.id}" not in response.text


async def test_orders_list_rejects_unknown_status(client, make_order):
    await _login(client)
    await make_order()

    assert (await client.get("/admin/orders?status=несуществующий")).status_code == 200


async def test_orders_list_searches_by_text(client, make_order):
    await _login(client)
    target = await make_order(from_city="Севастополь", to_city="Керчь")
    await make_order(from_city="Симферополь", to_city="Сочи")

    response = await client.get("/admin/orders?q=керчь")

    assert response.status_code == 200
    assert f"/orders/{target.id}" in response.text


# --- Ручное управление лентой ------------------------------------------------


async def test_hide_order_removes_it_from_public_feed(client, make_order, session):
    await _login(client)
    order = await make_order()

    response = await client.post(f"/admin/orders/{order.id}/hide")
    assert response.status_code == 303

    assert (await _reload_order(session, order.id)).status == OrderStatus.CANCELLED

    public = await client.get("/")
    assert f"/orders/{order.id}" not in public.text


async def test_hide_order_clears_problem_flag(client, make_order, session):
    """Скрытая заявка больше не должна висеть в списке жалоб."""
    await _login(client)
    order = await make_order()
    client.cookies.set("driver_id", "hider-driver")
    await client.post(f"/orders/{order.id}/take")
    await client.post(f"/orders/{order.id}/feedback", data={"reason": "wrong_data"})

    await client.post(f"/admin/orders/{order.id}/hide")

    reloaded = await _reload_order(session, order.id)
    assert reloaded.status == OrderStatus.CANCELLED
    assert reloaded.has_problem is False


async def test_unhide_returns_order_to_feed(client, make_order, session):
    await _login(client)
    order = await make_order(
        status=OrderStatus.CANCELLED, pickup_at=now_utc_naive() + timedelta(days=1)
    )

    response = await client.post(f"/admin/orders/{order.id}/unhide")
    assert response.status_code == 303

    assert (await _reload_order(session, order.id)).status == OrderStatus.NEW

    public = await client.get("/")
    assert f"/orders/{order.id}" in public.text


async def test_unhide_touches_only_cancelled_orders(client, make_order, session):
    """Возврат не должен сбрасывать, например, «договорились»."""
    await _login(client)
    order = await make_order(
        status=OrderStatus.AGREED, pickup_at=now_utc_naive() + timedelta(days=1)
    )

    await client.post(f"/admin/orders/{order.id}/unhide")

    assert (await _reload_order(session, order.id)).status == OrderStatus.AGREED


@pytest.mark.parametrize("action", ["hide", "unhide"])
async def test_order_actions_require_admin(client, make_order, action):
    order = await make_order()

    response = await client.post(f"/admin/orders/{order.id}/{action}")

    assert response.status_code == 303
    assert response.headers["location"] == "/admin/login"


# --- Очередь разбора ---------------------------------------------------------


async def _mark_failed(pending_id: int, *, attempts: int = 3,
                       error: str = "LLM: 429 too many requests") -> None:
    """Переводит запись очереди в FAILED в отдельной сессии.

    ``enqueue`` коммитит в своей сессии и возвращает отсоединённый объект;
    добавлять его в чужую сессию и потом перечитывать — прямой путь к
    ленивой загрузке вне greenlet.
    """
    async with SessionLocal() as fresh:
        row = await fresh.get(PendingMessage, pending_id)
        row.status = PendingStatus.FAILED
        row.attempts = attempts
        row.last_error = error
        fresh.add(row)
        await fresh.commit()


async def _reload_pending(pending_id: int) -> PendingMessage:
    async with SessionLocal() as fresh:
        return (
            await fresh.execute(select(PendingMessage).where(PendingMessage.id == pending_id))
        ).scalar_one()


async def test_queue_page_shows_failed_by_default(client):
    await _login(client)
    stuck = await pending.enqueue(chat_id=-1001, message_id=555, text="заявка, которую не разобрали")
    await _mark_failed(stuck.id)

    response = await client.get("/admin/queue")

    assert response.status_code == 200
    assert "заявка, которую не разобрали" in response.text
    assert "429 too many requests" in response.text


async def test_queue_page_switches_status(client):
    await _login(client)
    await pending.enqueue(chat_id=-1001, message_id=601, text="ожидает повтора")

    for status in ("pending", "failed", "done", "мусор"):
        assert (await client.get(f"/admin/queue?status={status}")).status_code == 200

    assert "ожидает повтора" in (await client.get("/admin/queue?status=pending")).text


async def test_queue_retry_resets_attempts(client):
    """Кнопка «повторить» — единственный способ вернуть FAILED в работу."""
    await _login(client)
    stuck = await pending.enqueue(chat_id=-1001, message_id=700, text="заявка")
    await _mark_failed(stuck.id, attempts=6)

    response = await client.post(f"/admin/queue/{stuck.id}/retry")
    assert response.status_code == 303
    assert response.headers["location"] == "/admin/queue?status=failed"

    revived = await _reload_pending(stuck.id)
    assert revived.status == PendingStatus.PENDING
    assert revived.attempts == 0
    assert revived.next_attempt_at <= now_utc_naive() + timedelta(seconds=5)


async def test_queue_retry_requires_admin(client):
    stuck = await pending.enqueue(chat_id=-1001, message_id=800, text="заявка")
    await _mark_failed(stuck.id)

    response = await client.post(f"/admin/queue/{stuck.id}/retry")

    assert response.headers["location"] == "/admin/login"
    assert (await _reload_pending(stuck.id)).status == PendingStatus.FAILED


async def test_admin_cookie_gives_no_privileges_on_public_pages(client, make_order):
    """Админ не брал заказ — значит контакты клиента ему тоже закрыты."""
    await _login(client)
    order = await make_order(client_phone="+79990000000", client_name="Иван Петров")

    page = await client.get(f"/orders/{order.id}")

    assert page.status_code == 200
    assert "+79990000000" not in page.text
