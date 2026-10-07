"""Уведомления в Telegram о новых заказах по фильтру водителя."""

from datetime import timedelta

import pytest
from sqlalchemy import select

from app.config import settings
from app.db.base import SessionLocal
from app.models import Driver, OrderSubscription, SubscriptionNotification
from app.services import subscriptions as svc
from app.services.subscriptions import BLOCKED, OK, RETRY, describe_filters, notify_once
from app.timeutil import now_msk_naive, now_utc_naive
from app.web.filters import Filters
from tests.test_telegram_login import BOT_TOKEN, _signed_params

KRASNODAR = (45.0355, 38.9753)
NEAR = (45.15, 39.05)  # ~14 км от Краснодара
SOCHI = (43.5855, 39.7231)


@pytest.fixture
def telegram_login_on(monkeypatch):
    monkeypatch.setattr(settings, "telegram_login_bot_token", BOT_TOKEN)
    monkeypatch.setattr(settings, "telegram_login_bot_username", "podacha_bot")
    monkeypatch.setattr(settings, "public_base_url", "https://example.test")


class Outbox:
    """Подмена отправки: запоминает сообщения, исход задаётся тестом."""

    def __init__(self, outcome=OK):
        self.outcome = outcome
        self.messages: list[tuple[int, str]] = []

    async def __call__(self, chat_id, text):
        self.messages.append((chat_id, text))
        return self.outcome


async def _subscribe(session, telegram_id=1001, *, since_minutes_ago=60, active=True, **params):
    filters = Filters(**{key: str(value) for key, value in params.items()})
    sub = OrderSubscription(
        telegram_id=telegram_id, params=filters.as_dict(), is_active=active,
        since=now_utc_naive() - timedelta(minutes=since_minutes_ago),
    )
    session.add(sub)
    await session.commit()
    return sub


async def _fresh_order(make_order, **kwargs):
    kwargs.setdefault("pickup_at", now_msk_naive() + timedelta(days=1))
    return await make_order(**kwargs)


# --- Кому что присылать -----------------------------------------------------------


async def test_matching_new_order_is_sent_with_link(session, make_order, monkeypatch):
    monkeypatch.setattr(settings, "public_base_url", "https://example.test")
    await _subscribe(session, from_city="Краснодар")
    order = await _fresh_order(make_order, from_city="Краснодар", to_city="Сочи", price="3000")
    box = Outbox()

    assert await notify_once(send=box) == 1

    (chat_id, text), = box.messages
    assert chat_id == 1001
    assert "Новый заказ по вашему фильтру" in text and "Краснодар → Сочи" in text
    assert "3000 ₽" in text and f"https://example.test/orders/{order.id}" in text


async def test_non_matching_order_is_not_sent(session, make_order):
    await _subscribe(session, from_city="Краснодар")
    await _fresh_order(make_order, from_city="Ялта", to_city="Керчь")
    box = Outbox()

    assert await notify_once(send=box) == 0 and box.messages == []


async def test_same_order_is_never_sent_twice(session, make_order):
    await _subscribe(session, from_city="Краснодар")
    await _fresh_order(make_order, from_city="Краснодар")
    box = Outbox()

    assert await notify_once(send=box) == 1
    assert await notify_once(send=box) == 0
    assert len(box.messages) == 1


async def test_orders_older_than_the_subscription_are_not_sent(session, make_order):
    """Подписка — про новые заказы: то, что водитель уже видит в ленте, не шлём."""
    order = await _fresh_order(make_order, from_city="Краснодар")
    await _subscribe(session, from_city="Краснодар", since_minutes_ago=-5)  # подписался «через 5 минут»
    box = Outbox()

    assert await notify_once(send=box) == 0
    assert order.id  # заказ существует, но он старше подписки


async def test_inactive_subscription_is_skipped(session, make_order):
    await _subscribe(session, from_city="Краснодар", active=False)
    await _fresh_order(make_order, from_city="Краснодар")

    assert await notify_once(send=Outbox()) == 0


async def test_taken_or_closed_orders_are_not_sent(session, make_order):
    from app.models import OrderStatus

    await _subscribe(session, from_city="Краснодар")
    await _fresh_order(make_order, from_city="Краснодар", taken_by_token="tg:5")
    await _fresh_order(make_order, from_city="Краснодар", status=OrderStatus.EXPIRED)

    assert await notify_once(send=Outbox()) == 0


async def test_each_driver_gets_only_their_own_matches(session, make_order):
    await _subscribe(session, 1, from_city="Краснодар")
    await _subscribe(session, 2, from_city="Ялта")
    await _fresh_order(make_order, from_city="Краснодар")
    await _fresh_order(make_order, from_city="Ялта")
    box = Outbox()

    assert await notify_once(send=box) == 2
    by_chat = {chat: text for chat, text in box.messages}
    assert "Краснодар →" in by_chat[1] and "Ялта" not in by_chat[1]
    assert "Ялта →" in by_chat[2] and "Краснодар" not in by_chat[2]


async def test_price_and_direction_filters_apply_together(session, make_order):
    await _subscribe(session, from_city="Краснодар", to_city="Сочи", price_min="5000")
    await _fresh_order(make_order, from_city="Краснодар", to_city="Сочи", price="3000")  # дёшево
    await _fresh_order(make_order, from_city="Краснодар", to_city="Анапа", price="9000")  # не туда
    good = await _fresh_order(make_order, from_city="Краснодар", to_city="Сочи", price="9000")
    box = Outbox()

    await notify_once(send=box)

    assert len(box.messages) == 1 and f"/orders/{good.id}" in box.messages[0][1]


# --- Радиус -------------------------------------------------------------------------


async def test_radius_subscription_catches_a_village_near_the_city(session, make_order):
    await _subscribe(session, from_city="Краснодар", from_radius="50")
    near = await _fresh_order(make_order, from_city="станица Динская", from_coords=NEAR)
    await _fresh_order(make_order, from_city="Сочи", from_coords=SOCHI)
    box = Outbox()

    await notify_once(send=box)

    assert len(box.messages) == 1 and f"/orders/{near.id}" in box.messages[0][1]


async def test_order_waits_for_geocoding_but_not_forever(session, make_order):
    """Пока координаты считаются, радиус-подписке заказ не виден — подождём."""
    await _subscribe(session, from_city="Краснодар", from_radius="50")
    order = await _fresh_order(make_order, from_city="Краснодар")  # без geo_checked_at
    box = Outbox()
    now = now_utc_naive()

    assert await notify_once(send=box, now=now) == 0  # только что создан
    assert await notify_once(send=box, now=now + timedelta(minutes=3)) == 1  # ждать надоело
    assert f"/orders/{order.id}" in box.messages[0][1]


# --- Сбои доставки --------------------------------------------------------------------


async def test_blocked_bot_switches_the_subscription_off_with_a_reason(session, make_order):
    sub = await _subscribe(session, from_city="Краснодар")
    await _fresh_order(make_order, from_city="Краснодар")

    await notify_once(send=Outbox(BLOCKED))

    async with SessionLocal() as db:
        saved = (await db.execute(select(OrderSubscription).where(OrderSubscription.id == sub.id))).scalar_one()
    assert saved.is_active is False and "Старт" in saved.error


async def test_temporary_failure_is_retried_without_duplicates(session, make_order):
    await _subscribe(session, from_city="Краснодар")
    await _fresh_order(make_order, from_city="Краснодар")
    flaky = Outbox(RETRY)

    assert await notify_once(send=flaky) == 0  # не дошло, запись об отправке не сделана
    assert await notify_once(send=Outbox(OK)) == 1  # дошло со второй попытки
    async with SessionLocal() as db:
        logged = (await db.execute(select(SubscriptionNotification))).scalars().all()
    assert len(logged) == 1


# --- Сообщения ------------------------------------------------------------------------


async def test_many_matches_are_one_digest_not_a_flood(session, make_order, monkeypatch):
    monkeypatch.setattr(settings, "subscription_max_lines", 3)
    monkeypatch.setattr(settings, "public_base_url", "https://example.test")
    await _subscribe(session, from_city="Краснодар")
    for _ in range(5):
        await _fresh_order(make_order, from_city="Краснодар")
    box = Outbox()

    assert await notify_once(send=box) == 1  # одно сообщение на пятерых

    text = box.messages[0][1]
    assert "Новых заказов по вашему фильтру: 5" in text and text.count("/orders/") == 3
    assert "и ещё 2" in text and "https://example.test/?from_city=" in text


def test_describe_filters_in_plain_russian():
    params = Filters(
        from_city="Краснодар", from_radius="50", to_city="Сочи", vehicle="minivan", price_min="5000"
    ).as_dict()

    assert describe_filters(params) == [
        "Откуда: Краснодар (+50 км)", "Куда: Сочи", "Тип авто: минивэн", "Цена: от 5000 до ∞ ₽",
    ]


def test_filter_without_a_city_is_not_a_valid_subscription():
    assert svc.has_route(Filters(from_city="Краснодар").as_dict())
    assert svc.has_route(Filters(to_city="Сочи").as_dict())
    assert not svc.has_route(Filters(price_min="5000", vehicle="car").as_dict())


# --- Веб: «Применить» и профиль ----------------------------------------------------------


async def _login(client, **overrides):
    await client.get("/auth/telegram", params=_signed_params(**overrides))


async def _driver_id(session):
    return (await session.execute(select(Driver.telegram_id))).scalar_one()


async def test_apply_with_checkbox_creates_subscription_and_shows_the_filtered_feed(
    client, session, telegram_login_on
):
    await _login(client)

    response = await client.post(
        "/filter", data={"from_city": "Краснодар", "from_radius": "50", "notify": "on", "q": ""}
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/?from_city=%D0%9A%D1%80%D0%B0%D1%81%D0%BD%D0%BE%D0%B4%D0%B0%D1%80&from_radius=50"
    sub = (await session.execute(select(OrderSubscription))).scalar_one()
    assert sub.telegram_id == await _driver_id(session) and sub.is_active
    assert sub.params["from_city"] == "Краснодар" and sub.params["from_radius"] == "50"


async def test_apply_without_checkbox_only_filters(client, session, telegram_login_on):
    await _login(client)

    response = await client.post("/filter", data={"from_city": "Ялта"})

    assert response.status_code == 303
    assert (await session.execute(select(OrderSubscription))).scalars().all() == []


async def test_unchecking_the_box_switches_notifications_off(client, session, telegram_login_on):
    await _login(client)
    await client.post("/filter", data={"from_city": "Ялта", "notify": "on"})

    await client.post("/filter", data={"from_city": "Ялта"})  # галочку сняли

    session.expire_all()
    assert (await session.execute(select(OrderSubscription))).scalar_one().is_active is False


async def test_filter_without_a_city_does_not_subscribe(client, session, telegram_login_on):
    await _login(client)

    response = await client.post("/filter", data={"price_min": "5000", "notify": "on"})

    assert response.status_code == 303 and "price_min=5000" in response.headers["location"]
    assert (await session.execute(select(OrderSubscription))).scalars().all() == []
    assert "укажите город" in (await client.get("/")).text


async def test_new_filter_replaces_the_old_one_and_resets_since(client, session, telegram_login_on):
    await _login(client)
    await client.post("/filter", data={"from_city": "Ялта", "notify": "on"})
    first = (await session.execute(select(OrderSubscription))).scalar_one()
    first_since = first.since

    await client.post("/filter", data={"to_city": "Керчь", "notify": "on"})

    session.expire_all()
    rows = (await session.execute(select(OrderSubscription))).scalars().all()
    assert len(rows) == 1  # одна подписка на водителя
    assert rows[0].params["to_city"] == "Керчь" and rows[0].params["from_city"] == ""
    assert rows[0].since >= first_since


async def test_guest_cannot_subscribe(client, session, telegram_login_on):
    response = await client.post("/filter", data={"from_city": "Ялта", "notify": "on"})

    assert response.status_code == 303 and response.headers["location"].startswith("/login")
    assert (await session.execute(select(OrderSubscription))).scalars().all() == []


async def test_filter_form_differs_for_guest_and_driver(client, telegram_login_on):
    guest = (await client.get("/")).text
    assert 'method="get" action="/"' in guest and 'name="notify"' not in guest
    assert "Войдите, чтобы получать новые заказы" in guest

    await _login(client)
    driver = (await client.get("/")).text
    assert 'method="post" action="/filter"' in driver
    assert 'name="notify" checked' in driver  # по умолчанию включена


async def test_checkbox_reflects_a_switched_off_subscription(client, telegram_login_on):
    await _login(client)
    await client.post("/filter", data={"from_city": "Ялта", "notify": "on"})
    await client.post("/filter", data={"from_city": "Ялта"})

    page = (await client.get("/")).text

    assert 'name="notify" checked' not in page and 'name="notify"' in page


async def test_profile_shows_subscription_and_toggle_buttons(client, session, telegram_login_on):
    await _login(client)
    empty = (await client.get("/profile")).text
    assert "Хотите получать новые заказы в Telegram?" in empty

    await client.post("/filter", data={"from_city": "Краснодар", "from_radius": "50", "to_city": "Сочи", "notify": "on"})
    page = (await client.get("/profile")).text
    assert "Включены" in page and "Откуда: Краснодар (+50 км)" in page and "Куда: Сочи" in page
    assert "/?from_city=" in page and 'action="/subscription/off"' in page

    off = await client.post("/subscription/off")
    assert off.status_code == 303 and off.headers["location"] == "/profile"
    page = (await client.get("/profile")).text
    assert "Выключены" in page and 'action="/subscription/on"' in page

    await client.post("/subscription/on")
    assert "Включены" in (await client.get("/profile")).text


async def test_unknown_subscription_action_is_404(client, telegram_login_on):
    await _login(client)

    assert (await client.post("/subscription/delete-everything")).status_code == 404


# --- Заявку удалил диспетчер: уведомление исчезает и у водителя ---------------------------------------


class Telegram:
    """Подмена Bot API: отправка выдаёт номера сообщений, удаление и правка запоминаются."""

    def __init__(self, delete_outcome=OK, edit_outcome=OK):
        self.next_id = 500
        self.deleted: list[tuple[int, int]] = []
        self.edited: list[tuple[int, int, str]] = []
        self.delete_outcome, self.edit_outcome = delete_outcome, edit_outcome

    async def send(self, chat_id, text):
        from app.services.subscriptions import SendOutcome

        self.next_id += 1
        return SendOutcome(OK, self.next_id)

    async def delete(self, chat_id, message_id):
        self.deleted.append((chat_id, message_id))
        return self.delete_outcome

    async def edit(self, chat_id, message_id, text):
        self.edited.append((chat_id, message_id, text))
        return self.edit_outcome


async def _withdraw(session, order, *, action="cancelled_message_deleted"):
    from app.models import ActionLog, ActorType, OrderStatus

    order.status = OrderStatus.CANCELLED
    session.add(order)
    session.add(ActionLog(order_id=order.id, actor=ActorType.DISPATCHER, action=action))
    await session.commit()


async def _notified(session, make_order, tg, **order_kwargs):
    await _subscribe(session, from_city="Симферополь")
    order = await _fresh_order(make_order, **order_kwargs)
    await notify_once(send=tg.send)
    return order


async def test_message_is_deleted_when_the_dispatcher_deletes_the_order(session, make_order):
    from app.services.subscriptions import retract_withdrawn

    tg = Telegram()
    order = await _notified(session, make_order, tg)
    assert await retract_withdrawn(delete=tg.delete, edit=tg.edit) == 0  # заказ жив — ничего не трогаем
    await _withdraw(session, order)

    assert await retract_withdrawn(delete=tg.delete, edit=tg.edit) == 1

    assert tg.deleted == [(1001, 501)] and tg.edited == []
    assert await retract_withdrawn(delete=tg.delete, edit=tg.edit) == 0  # повторно не удаляем
    assert len(tg.deleted) == 1


async def test_digest_is_rewritten_without_the_withdrawn_order(session, make_order):
    from app.services.subscriptions import retract_withdrawn

    tg = Telegram()
    await _subscribe(session, from_city="Симферополь")
    first = await _fresh_order(make_order, to_city="Ялта")
    await _fresh_order(make_order, to_city="Керчь")
    await notify_once(send=tg.send)  # оба в одном сообщении
    await _withdraw(session, first)

    await retract_withdrawn(delete=tg.delete, edit=tg.edit)

    assert tg.deleted == []
    (chat, message_id, text), = tg.edited
    assert (chat, message_id) == (1001, 501)
    assert "Керчь" in text and "Ялта" not in text  # осталась только живая заявка


async def test_other_cancellations_do_not_remove_the_message(session, make_order):
    """Дубль, скрытие вручную, правка: заказ мог остаться доступным в другой заявке — не трогаем."""
    from app.services.subscriptions import retract_withdrawn

    tg = Telegram()
    order = await _notified(session, make_order, tg)
    await _withdraw(session, order, action="duplicate_cancelled")

    assert await retract_withdrawn(delete=tg.delete, edit=tg.edit) == 0
    assert tg.deleted == []


async def test_network_failure_is_retried_on_the_next_pass(session, make_order):
    from app.services.subscriptions import RETRY, retract_withdrawn

    tg = Telegram(delete_outcome=RETRY)
    order = await _notified(session, make_order, tg)
    await _withdraw(session, order)

    assert await retract_withdrawn(delete=tg.delete, edit=tg.edit) == 0
    tg.delete_outcome = OK
    assert await retract_withdrawn(delete=tg.delete, edit=tg.edit) == 1
    assert len(tg.deleted) == 2


async def test_already_deleted_message_is_not_retried_forever(session, make_order):
    from app.services.subscriptions import GONE, retract_withdrawn

    tg = Telegram(delete_outcome=GONE)  # водитель сам удалил чат/сообщение
    order = await _notified(session, make_order, tg)
    await _withdraw(session, order)

    assert await retract_withdrawn(delete=tg.delete, edit=tg.edit) == 1
    assert await retract_withdrawn(delete=tg.delete, edit=tg.edit) == 0


async def test_messages_older_than_telegram_allows_are_left_alone(session, make_order):
    from app.services.subscriptions import retract_withdrawn

    tg = Telegram()
    order = await _notified(session, make_order, tg)
    await _withdraw(session, order)

    assert await retract_withdrawn(delete=tg.delete, edit=tg.edit, now=now_utc_naive() + timedelta(hours=48)) == 0
    assert tg.deleted == []


async def test_notifications_without_a_message_id_are_ignored(session, make_order):
    """Отправленные до появления поля: номера сообщения нет, удалять нечего."""
    from app.services.subscriptions import retract_withdrawn

    order = await _fresh_order(make_order)
    await _subscribe(session, from_city="Симферополь")
    await notify_once(send=Outbox())  # обычная строка «ok», без номера
    await _withdraw(session, order)
    tg = Telegram()

    assert await retract_withdrawn(delete=tg.delete, edit=tg.edit) == 0


async def test_real_bot_api_calls_use_the_right_methods(monkeypatch):
    from app.services import subscriptions

    calls = []

    class FakeClient:
        def __init__(self, **kwargs): ...
        async def __aenter__(self): return self
        async def __aexit__(self, *exc): return False

        async def post(self, url, json):
            calls.append((url.rsplit("/", 1)[1], json))
            import httpx

            if json.get("text") == "same":
                return httpx.Response(400, text='{"description":"Bad Request: message is not modified"}')
            if json.get("message_id") == 404:
                return httpx.Response(400, text='{"description":"Bad Request: message to delete not found"}')
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 77}})

    monkeypatch.setattr(subscriptions.httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(settings, "alert_bot_token", "123:abc")

    sent = await subscriptions.send_message(5, "привет")
    assert sent == OK and sent.message_id == 77
    assert await subscriptions.delete_message(5, 77) == OK
    assert await subscriptions.delete_message(5, 404) == subscriptions.GONE
    assert await subscriptions.edit_message(5, 77, "same") == OK  # «не изменилось» — не ошибка
    assert [name for name, _ in calls] == ["sendMessage", "deleteMessage", "deleteMessage", "editMessageText"]
