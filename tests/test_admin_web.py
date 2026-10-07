"""Админка владельца: доступ, отчёты, ручное управление лентой и очередью.

Админка — единственное место, где видно качество разбора и застрявшие
сообщения, поэтому доступ к ней проверяется особенно придирчиво: сайт
публичный, а здесь и телефоны клиентов, и статистика расходов на LLM.
Открывается она только владельцу, вошедшему через Telegram; для всех
остальных её нет вовсе (404, без страницы входа).
"""

import hashlib
import hmac
import time
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.config import settings
from app.db.base import SessionLocal
from app.models import Order, OrderStatus, PendingMessage, PendingStatus
from app.telegram import pending
from app.timeutil import now_utc_naive

BOT_TOKEN = "123456:test-bot-token"
OWNER_ID = 555000111  # он же в ADMIN_TELEGRAM_IDS тестового окружения (conftest.py)
STRANGER_ID = 777000222


@pytest.fixture(autouse=True)
def telegram_login_on(monkeypatch):
    monkeypatch.setattr(settings, "telegram_login_bot_token", BOT_TOKEN)
    monkeypatch.setattr(settings, "telegram_login_bot_username", "podacha_bot")
    assert settings.admin_enabled is True


async def _login(client, telegram_id: int = OWNER_ID):
    """Входит через Telegram Login Widget с настоящей подписью."""
    fields = {
        "id": str(telegram_id),
        "first_name": "Тест",
        "auth_date": str(int(time.time())),
    }
    check = chr(10).join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hashlib.sha256(BOT_TOKEN.encode("utf-8")).digest()
    fields["hash"] = hmac.new(secret, check.encode("utf-8"), hashlib.sha256).hexdigest()
    response = await client.get("/auth/telegram", params=fields)
    assert response.status_code == 303
    return response


async def _reload_order(session, order_id: int) -> Order:
    session.expire_all()
    return (await session.execute(select(Order).where(Order.id == order_id))).scalar_one()


# --- Доступ ------------------------------------------------------------------

ADMIN_PATHS = ("/admin", "/admin/", "/admin/orders", "/admin/queue", "/admin/clarify", "/admin/users", "/admin/login")


async def test_guest_gets_404_everywhere(client):
    """Гость не должен даже узнать, что админка существует: ни формы входа, ни редиректа."""
    for path in ADMIN_PATHS:
        response = await client.get(path)
        assert response.status_code == 404, path
        assert "location" not in response.headers, path


async def test_logged_in_stranger_gets_404(client):
    """Вошёл через Telegram, но его id нет в ADMIN_TELEGRAM_IDS — админки для него нет."""
    await _login(client, STRANGER_ID)

    for path in ADMIN_PATHS:
        assert (await client.get(path)).status_code == 404, path


async def test_owner_gets_dashboard(client):
    await _login(client)

    for path in ("/admin", "/admin/orders", "/admin/queue", "/admin/clarify", "/admin/users"):
        assert (await client.get(path)).status_code == 200, path


async def test_password_login_no_longer_exists(client):
    """Пароля больше нет: форма входа и logout отвечают 404 даже владельцу."""
    await _login(client)

    assert (await client.get("/admin/login")).status_code == 404
    assert (await client.post("/admin/login", data={"password": "test-admin-password"})).status_code == 404
    assert (await client.post("/admin/logout")).status_code == 404


async def test_old_admin_cookie_gives_nothing(client):
    client.cookies.set("admin_session", "9999999999.deadbeef")

    assert (await client.get("/admin")).status_code == 404


async def test_forged_session_for_owner_id_is_rejected(client):
    """Подделать вход владельца, не зная SESSION_SECRET, нельзя."""
    client.cookies.set("driver_session", f"{OWNER_ID}.{int(time.time()) + 3600}.{'0' * 64}")

    assert (await client.get("/admin")).status_code == 404


async def test_header_tab_only_for_owner(client):
    guest = await client.get("/")
    assert "Админка" not in guest.text

    await _login(client, STRANGER_ID)
    assert "Админка" not in (await client.get("/")).text

    client.cookies.clear()
    await _login(client)
    assert "Админка" in (await client.get("/")).text


async def test_admin_is_absent_without_ids(client, monkeypatch):
    """ADMIN_TELEGRAM_IDS пуст — админки нет ни у кого, в том числе у вошедших."""
    await _login(client)
    monkeypatch.setattr(settings, "admin_telegram_ids", "")

    for path in ADMIN_PATHS:
        assert (await client.get(path)).status_code == 404, path


async def test_admin_is_absent_when_telegram_login_is_off(client, monkeypatch):
    """Без входа через Telegram админку открывать нечем — она выключена."""
    await _login(client)
    monkeypatch.setattr(settings, "telegram_login_bot_token", "")

    assert (await client.get("/admin")).status_code == 404


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


# --- Статистика: заказы по дням и направления --------------------------------


async def _created(session, order, days_ago: int) -> None:
    order.created_at = now_utc_naive() - timedelta(days=days_ago, hours=1)
    session.add(order)
    await session.commit()


async def test_orders_per_day_counts_days_and_skips_cancelled(make_order, session):
    from app.services import reporting

    for days_ago in (0, 0, 2):
        await _created(session, await make_order(), days_ago)
    hidden = await make_order(status=OrderStatus.CANCELLED, pickup_at=now_utc_naive() + timedelta(days=1))
    await _created(session, hidden, 0)
    await _created(session, await make_order(), 40)  # вне периода

    daily = await reporting.orders_per_day(days=7)

    assert daily["total"] == 3
    assert [row["count"] for row in daily["rows"]] == [2, 0, 1, 0, 0, 0, 0]  # новые сверху, нули остаются
    assert daily["today"] == 2
    assert daily["peak"] == 2
    assert daily["rows"][0]["share"] == 1.0


async def test_popular_routes_merge_city_spellings(make_order, session):
    from app.services import reporting

    for origin in ("Мин. Воды", "Минводы", "Минеральные Воды"):
        await _created(session, await make_order(from_city=origin, to_city="Кисловодск", price="2000"), 1)
    await _created(session, await make_order(from_city="Симферополь", to_city="Ялта", price="3000"), 1)

    stats = await reporting.popular_routes(days=7)

    top = stats["routes"][0]
    assert top["count"] == 3
    assert top["to"] == "Кисловодск"
    assert top["avg_price"] == 2000
    assert stats["from_cities"][0]["count"] == 3
    assert len(stats["routes"]) == 2


async def test_dashboard_shows_daily_and_routes(client, make_order, session):
    await _login(client)
    await _created(session, await make_order(from_city="Симферополь", to_city="Ялта"), 0)

    response = await client.get("/admin?days=30")

    assert response.status_code == 200
    assert "Заказов в день" in response.text
    assert "Популярные направления" in response.text
    assert "Симферополь → Ялта" in response.text


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

    assert response.status_code == 404


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

    assert response.status_code == 404
    assert (await _reload_pending(stuck.id)).status == PendingStatus.FAILED


async def test_admin_gives_no_privileges_on_public_pages(client, make_order):
    """Админ не брал заказ — значит контакты клиента ему тоже закрыты."""
    await _login(client)
    order = await make_order(client_phone="+79990000000", client_name="Иван Петров")

    page = await client.get(f"/orders/{order.id}")

    assert page.status_code == 200
    assert "+79990000000" not in page.text


async def test_hidden_breakdown_counts_reasons(session):
    from app.models import ActionLog, ActorType
    from app.services import reporting

    for action, times in (("duplicate_skipped", 3), ("duplicate_cancelled", 1), ("cancelled_message_deleted", 2),
                          ("order_created", 5)):
        for _ in range(times):
            session.add(ActionLog(actor=ActorType.SYSTEM, action=action))
    await session.commit()

    stats = await reporting.hidden_breakdown(days=7)

    by_label = {row["label"]: row["count"] for row in stats["reasons"]}
    assert stats["created"] == 5
    assert by_label[reporting.HIDDEN_REASONS["duplicate_skipped"]] == 3
    assert by_label[reporting.HIDDEN_REASONS["duplicate_cancelled"]] == 1
    assert by_label[reporting.HIDDEN_REASONS["cancelled_message_deleted"]] == 2
    assert by_label[reporting.HIDDEN_REASONS["hidden_by_admin"]] == 0


async def test_dashboard_shows_hidden_reasons(client):
    await _login(client)

    response = await client.get("/admin")

    assert response.status_code == 200
    assert "Скрытые заявки и дубли" in response.text
    assert "Дубль остановлен при приёме" in response.text


# --- Раздел «Нужно уточнить» -------------------------------------------------------------------------------


async def _unclear(session, make_order, *, details=None, **fields):
    from app.models import ActionLog, ActorType

    order = await make_order(**fields)
    order.status = OrderStatus.NEEDS_CLARIFICATION
    session.add(order)
    if details is not None:
        session.add(ActionLog(order_id=order.id, actor=ActorType.DISPATCHER, action="order_created", details=details))
    await session.commit()
    return order


async def test_clarify_page_lists_only_orders_to_clarify_with_the_reason(client, make_order, session):
    await _login(client)
    unclear = await _unclear(session, make_order, details="missing_fields=['не указана точная стоимость']")
    fine = await make_order()

    response = await client.get("/admin/clarify")

    assert response.status_code == 200
    assert f"/orders/{unclear.id}" in response.text
    assert f"/orders/{fine.id}" not in response.text
    assert "не указана точная стоимость" in response.text  # причина, записанная моделью
    assert "Подтвердить" in response.text and "Скрыть" in response.text


async def test_clarify_reason_falls_back_to_the_missing_fields_of_the_order(client, make_order, session):
    await _login(client)
    order = await _unclear(session, make_order, price="")
    order.client_price = None
    session.add(order)
    await session.commit()

    response = await client.get("/admin/clarify")

    assert "не указана цена" in response.text


async def test_clarify_nav_shows_the_count_on_every_admin_page(client, make_order, session):
    await _login(client)
    await _unclear(session, make_order)
    await _unclear(session, make_order)

    for path in ("/admin", "/admin/orders", "/admin/queue", "/admin/clarify", "/admin/users"):
        page = await client.get(path)
        assert 'href="/admin/clarify"' in page.text and '<span class="count">2</span>' in page.text, path


async def test_clarify_empty_state(client):
    await _login(client)

    assert "Заявок, которые нужно уточнить, нет" in (await client.get("/admin/clarify")).text


async def test_confirm_removes_the_mark_and_keeps_the_order_in_the_feed(client, make_order, session):
    await _login(client)
    order = await _unclear(session, make_order)

    response = await client.post(f"/admin/orders/{order.id}/confirm")

    assert response.status_code == 303
    assert (await _reload_order(session, order.id)).status == OrderStatus.NEW
    assert f"/orders/{order.id}" in (await client.get("/")).text
    assert "Заявок, которые нужно уточнить, нет" in (await client.get("/admin/clarify")).text


async def test_confirm_touches_only_orders_that_need_clarification(client, make_order, session):
    await _login(client)
    cancelled = await make_order(status=OrderStatus.CANCELLED, pickup_at=now_utc_naive() + timedelta(days=1))

    await client.post(f"/admin/orders/{cancelled.id}/confirm")

    assert (await _reload_order(session, cancelled.id)).status == OrderStatus.CANCELLED


async def test_clarify_actions_require_admin(client, make_order, session):
    order = await _unclear(session, make_order)

    assert (await client.get("/admin/clarify")).status_code == 404
    assert (await client.post(f"/admin/orders/{order.id}/confirm")).status_code == 404
    assert (await _reload_order(session, order.id)).status == OrderStatus.NEEDS_CLARIFICATION


# --- Раздел «Пользователи» ------------------------------------------------------------------------------------


async def _driver(session, telegram_id=900001, **fields):
    from app.models import Driver

    driver = Driver(
        telegram_id=telegram_id, username=fields.pop("username", "ivan_driver"),
        first_name=fields.pop("first_name", "Иван"), last_name=fields.pop("last_name", "Петров"),
        last_login_at=now_utc_naive() - timedelta(hours=2), **fields,
    )
    session.add(driver)
    await session.commit()
    return driver


async def test_users_page_lists_everyone_with_short_stats(client, make_order, session):
    await _login(client)
    driver = await _driver(session)
    await make_order(status=OrderStatus.COMPLETED, price="10000", taken_by_token=driver.token,
                     pickup_at=now_utc_naive() - timedelta(days=1))
    await make_order(status=OrderStatus.AGREED, price="5000", taken_by_token=driver.token)
    await _driver(session, telegram_id=900002, username=None, first_name="Мария", last_name=None)

    response = await client.get("/admin/users")

    assert response.status_code == 200
    assert "Иван Петров" in response.text and "@ivan_driver" in response.text and "Мария" in response.text
    assert "10 000 ₽" in response.text  # сумма выполненных, не «в работе»
    assert f"/admin/users/{driver.telegram_id}" in response.text
    assert "зарегистрировано" in response.text


async def test_users_summary_counts(client, session):
    from app.models import Driver

    await _login(client)
    await _driver(session)
    old = Driver(telegram_id=900003, first_name="Старый")
    session.add(old)
    await session.commit()
    old.created_at = now_utc_naive() - timedelta(days=60)
    session.add(old)
    await session.commit()

    from app.services import reporting

    summary = (await reporting.users_overview())["summary"]

    # Вошедший владелец — тоже пользователь: он + новый водитель + «старый» без входов.
    assert summary["total"] == 3 and summary["new_week"] == 2 and summary["active_week"] == 2


async def test_users_search_and_sort(client, session):
    await _login(client)
    await _driver(session, telegram_id=900010, username="alpha", first_name="Анна")
    await _driver(session, telegram_id=900011, username="beta", first_name="Борис")

    only_beta = await client.get("/admin/users?q=@beta")
    assert "Борис" in only_beta.text and "Анна" not in only_beta.text
    by_id = await client.get("/admin/users?q=900010")
    assert "Анна" in by_id.text and "Борис" not in by_id.text
    assert (await client.get("/admin/users?sort=мусор")).status_code == 200


async def test_user_card_shows_profile_stats_orders_and_history(client, make_order, session):
    await _login(client)
    driver = await _driver(session)
    order = await make_order(status=OrderStatus.AGREED, price="7000", taken_by_token=driver.token)
    order.has_problem, order.problem_note = True, "диспетчер не отвечает"
    session.add(order)
    from app.models import ActionLog, ActorType

    session.add(ActionLog(order_id=order.id, actor=ActorType.DRIVER, action="order_agreed"))
    await session.commit()

    response = await client.get(f"/admin/users/{driver.telegram_id}")

    assert response.status_code == 200
    assert "Иван Петров" in response.text and str(driver.telegram_id) in response.text
    assert f"/orders/{order.id}" in response.text
    assert "диспетчер не отвечает" in response.text  # жалоба
    assert "order_agreed" in response.text  # история действий
    assert "https://t.me/ivan_driver" in response.text  # кнопка «Написать в Telegram»
    assert "Снять с водителя" in response.text


async def test_user_card_without_username_offers_chat_by_id(client, session):
    await _login(client)
    driver = await _driver(session, username=None)

    response = await client.get(f"/admin/users/{driver.telegram_id}")

    assert f"tg://user?id={driver.telegram_id}" in response.text


async def test_unknown_user_is_404(client):
    await _login(client)

    assert (await client.get("/admin/users/123456789")).status_code == 404


async def test_admin_can_switch_a_users_notifications_off_and_on(client, session):
    from app.models import OrderSubscription

    await _login(client)
    driver = await _driver(session)
    session.add(OrderSubscription(telegram_id=driver.telegram_id, params={"from_city": "Сочи"}, is_active=True,
                                  since=now_utc_naive()))
    await session.commit()

    assert (await client.post(f"/admin/users/{driver.telegram_id}/subscription/off")).status_code == 303
    async with SessionLocal() as fresh:
        sub = (await fresh.execute(select(OrderSubscription))).scalar_one()
        assert sub.is_active is False
    page = await client.get(f"/admin/users/{driver.telegram_id}")
    assert "Включить уведомления" in page.text and "Откуда: Сочи" in page.text

    await client.post(f"/admin/users/{driver.telegram_id}/subscription/on")
    async with SessionLocal() as fresh:
        assert (await fresh.execute(select(OrderSubscription))).scalar_one().is_active is True

    assert (await client.post(f"/admin/users/{driver.telegram_id}/subscription/мусор")).status_code == 404


async def test_admin_can_release_an_order_from_a_driver(client, make_order, session):
    await _login(client)
    driver = await _driver(session)
    order = await make_order(status=OrderStatus.AGREED, taken_by_token=driver.token)

    response = await client.post(f"/admin/users/{driver.telegram_id}/release/{order.id}")

    assert response.status_code == 303
    freed = await _reload_order(session, order.id)
    assert freed.taken_by_token is None and freed.status == OrderStatus.NEW


async def test_release_works_only_for_that_users_order(client, make_order, session):
    await _login(client)
    driver = await _driver(session)
    other = await make_order(status=OrderStatus.AGREED, taken_by_token="tg:555")

    await client.post(f"/admin/users/{driver.telegram_id}/release/{other.id}")

    assert (await _reload_order(session, other.id)).taken_by_token == "tg:555"


@pytest.mark.parametrize("method,path", [
    ("get", "/admin/users"), ("get", "/admin/users/1"),
    ("post", "/admin/users/1/subscription/off"), ("post", "/admin/users/1/release/1"),
])
async def test_users_section_requires_admin(client, method, path):
    assert (await getattr(client, method)(path)).status_code == 404
