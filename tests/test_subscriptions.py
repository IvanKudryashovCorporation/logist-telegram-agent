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
        self.markups: list[dict | None] = []

    async def __call__(self, chat_id, text, reply_markup=None):
        self.messages.append((chat_id, text))
        self.markups.append(reply_markup)
        return self.outcome


def _taken_ids(box, index: int) -> list[int]:
    """Какие заказы пришли в сообщении: номера из кнопок «Взять» (в тексте ссылок на заказы нет)."""
    return [
        int(button["callback_data"].removeprefix("take:"))
        for row in box.markups[index]["inline_keyboard"]
        for button in row
        if button.get("callback_data", "").startswith("take:")
    ]


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


async def test_matching_new_order_is_sent_with_buttons_not_a_link_in_the_text(session, make_order, monkeypatch):
    monkeypatch.setattr(settings, "public_base_url", "https://example.test")
    await _subscribe(session, from_city="Краснодар")
    order = await _fresh_order(make_order, from_city="Краснодар", to_city="Сочи", price="3000")
    box = Outbox()

    assert await notify_once(send=box) == 1

    (chat_id, text), = box.messages
    assert chat_id == 1001
    assert "Новый заказ по вашему фильтру" in text and "Краснодар → Сочи" in text and "3000 ₽" in text
    assert "/orders/" not in text  # водитель нажимает кнопки, а не ссылку на заказ
    buttons = [b for row in box.markups[0]["inline_keyboard"] for b in row]
    assert {"text": "✅ Взять заказ", "callback_data": f"take:{order.id}"} in buttons
    chat = next(b for b in buttons if "Написать диспетчеру" in b["text"])
    assert chat["url"].startswith("https://t.me/dispatcher_test?text=")  # сразу чат с диспетчером
    assert any(b.get("url") == f"https://example.test/orders/{order.id}" for b in buttons)  # «Подробнее»


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

    assert len(box.messages) == 1 and _taken_ids(box, 0) == [good.id]


# --- Радиус -------------------------------------------------------------------------


async def test_radius_subscription_catches_a_village_near_the_city(session, make_order):
    await _subscribe(session, from_city="Краснодар", from_radius="50")
    near = await _fresh_order(make_order, from_city="станица Динская", from_coords=NEAR)
    await _fresh_order(make_order, from_city="Сочи", from_coords=SOCHI)
    box = Outbox()

    await notify_once(send=box)

    assert len(box.messages) == 1 and _taken_ids(box, 0) == [near.id]


async def test_order_waits_for_geocoding_but_not_forever(session, make_order):
    """Пока координаты считаются, радиус-подписке заказ не виден — подождём."""
    await _subscribe(session, from_city="Краснодар", from_radius="50")
    order = await _fresh_order(make_order, from_city="Краснодар")  # без geo_checked_at
    box = Outbox()
    now = now_utc_naive()

    assert await notify_once(send=box, now=now) == 0  # только что создан
    assert await notify_once(send=box, now=now + timedelta(minutes=3)) == 1  # ждать надоело
    assert _taken_ids(box, 0) == [order.id]


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


async def test_each_order_is_its_own_message_with_its_own_buttons(session, make_order):
    await _subscribe(session, from_city="Краснодар")
    first = await _fresh_order(make_order, from_city="Краснодар", to_city="Сочи")
    second = await _fresh_order(make_order, from_city="Краснодар", to_city="Анапа")
    box = Outbox()

    assert await notify_once(send=box) == 2  # по сообщению на заказ

    assert len(box.messages) == 2
    assert all("Новый заказ по вашему фильтру" in text for _chat, text in box.messages)
    assert "Сочи" in box.messages[0][1] and "Анапа" in box.messages[1][1]
    assert [_taken_ids(box, 0), _taken_ids(box, 1)] == [[first.id], [second.id]]  # у каждого свои кнопки
    async with SessionLocal() as fresh:
        rows = (await fresh.execute(select(SubscriptionNotification))).scalars().all()
    assert len(rows) == 2 and {r.order_id for r in rows} == {first.id, second.id}


async def test_a_flood_is_cut_to_separate_messages_plus_one_tail_message(session, make_order, monkeypatch):
    monkeypatch.setattr(settings, "subscription_max_lines", 3)
    monkeypatch.setattr(settings, "public_base_url", "https://example.test")
    await _subscribe(session, from_city="Краснодар")
    for _ in range(5):
        await _fresh_order(make_order, from_city="Краснодар")
    box = Outbox()

    assert await notify_once(send=box) == 4  # 3 отдельных + «и ещё 2»

    assert [markup is not None for markup in box.markups] == [True, True, True, False]
    tail = box.messages[3][1]
    assert "И ещё подходящих заказов по вашему фильтру: 2" in tail and "https://example.test/?from_city=" in tail
    assert await notify_once(send=box) == 0  # хвост не приходит заново на следующем проходе


async def test_failure_in_the_middle_keeps_the_rest_for_the_next_pass(session, make_order):
    await _subscribe(session, from_city="Краснодар")
    for _ in range(3):
        await _fresh_order(make_order, from_city="Краснодар")

    class FlakyOutbox(Outbox):
        async def __call__(self, chat_id, text, reply_markup=None):
            if len(self.messages) == 1:  # второе сообщение не уходит
                self.messages.append((chat_id, text))
                self.markups.append(reply_markup)
                return RETRY
            return await super().__call__(chat_id, text, reply_markup)

    box = FlakyOutbox()
    await notify_once(send=box)
    retry = Outbox()

    await notify_once(send=retry)

    sent_now = [i for index in range(len(retry.messages)) for i in _taken_ids(retry, index)]
    sent_before = _taken_ids(box, 0)
    assert len(sent_before) == 1 and len(sent_now) == 2  # первый не дублируется, два оставшихся доходят
    assert not set(sent_before) & set(sent_now)


def test_describe_filters_in_plain_russian():
    params = Filters(
        from_city="Краснодар", from_radius="50", to_city="Сочи", vehicle="minivan", price_min="5000"
    ).as_dict()

    assert describe_filters(params) == [
        "Откуда: Краснодар (+50 км)", "Куда: Сочи", "Тип авто: минивэн", "Цена: от 5000 до ∞ ₽",
    ]


def test_any_real_criterion_makes_a_valid_filter_but_an_empty_one_does_not():
    assert svc.has_criteria(Filters(from_city="Краснодар").as_dict())
    assert svc.has_criteria(Filters(to_city="Сочи").as_dict())
    assert svc.has_criteria(Filters(price_min="5000").as_dict())  # без города — можно
    assert svc.has_criteria(Filters(vehicle="minivan").as_dict())
    assert svc.has_criteria(Filters(date_from="2026-10-10").as_dict())
    assert svc.has_criteria(Filters(time_from="08:00").as_dict())
    assert not svc.has_criteria(Filters().as_dict())
    assert not svc.has_criteria(Filters(from_radius="50").as_dict())  # радиус без города ничего не отсеивает
    assert not svc.has_criteria(Filters(price_min="мусор").as_dict())  # мусор нормализуется в пустоту


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


async def test_filter_without_a_city_can_be_saved(client, session, telegram_login_on):
    await _login(client)

    response = await client.post("/filter", data={"price_min": "5000", "vehicle": "minivan", "notify": "on"})

    assert response.status_code == 303 and "price_min=5000" in response.headers["location"]
    sub = (await session.execute(select(OrderSubscription))).scalar_one()
    assert sub.is_active and sub.params["price_min"] == "5000" and sub.params["vehicle"] == "minivan"
    assert "добавлен в «Мои фильтры»" in (await client.get("/")).text
    page = (await client.get("/filters")).text
    assert "Цена: от 5000 до ∞ ₽" in page and "Тип авто: минивэн" in page


async def test_empty_filter_is_applied_but_not_saved(client, session, telegram_login_on):
    await _login(client)

    response = await client.post("/filter", data={"notify": "on"})

    assert response.status_code == 303
    assert (await session.execute(select(OrderSubscription))).scalars().all() == []
    assert "хотя бы одно условие" in (await client.get("/")).text


async def test_a_filter_without_a_city_catches_matching_orders_in_any_city(session, make_order):
    await _subscribe(session, price_min="10000")
    cheap = await _fresh_order(make_order, from_city="Сочи", to_city="Анапа", price="3000")
    dear_a = await _fresh_order(make_order, from_city="Сочи", to_city="Москва", price="25000")
    dear_b = await _fresh_order(make_order, from_city="Казань", to_city="Уфа", price="12000")
    box = Outbox()

    await notify_once(send=box)

    caught = [i for index in range(len(box.messages)) for i in _taken_ids(box, index)]
    assert sorted(caught) == sorted([dear_a.id, dear_b.id]) and cheap.id not in caught


async def test_guest_cannot_subscribe(client, session, telegram_login_on):
    response = await client.post("/filter", data={"from_city": "Ялта", "notify": "on"})

    assert response.status_code == 303 and response.headers["location"].startswith("/login")
    assert (await session.execute(select(OrderSubscription))).scalars().all() == []


# --- Заявку удалил диспетчер: уведомление исчезает и у водителя ---------------------------------------


class Telegram:
    """Подмена Bot API: отправка выдаёт номера сообщений, удаление и правка запоминаются."""

    def __init__(self, delete_outcome=OK, edit_outcome=OK):
        self.next_id = 500
        self.deleted: list[tuple[int, int]] = []
        self.edited: list[tuple[int, int, str]] = []
        self.edit_markups: list[dict | None] = []
        self.delete_outcome, self.edit_outcome = delete_outcome, edit_outcome

    async def send(self, chat_id, text, reply_markup=None):
        from app.services.subscriptions import SendOutcome

        self.next_id += 1
        return SendOutcome(OK, self.next_id)

    async def delete(self, chat_id, message_id):
        self.deleted.append((chat_id, message_id))
        return self.delete_outcome

    async def edit(self, chat_id, message_id, text, reply_markup=None):
        self.edited.append((chat_id, message_id, text))
        self.edit_markups.append(reply_markup)
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


async def test_old_digest_is_rewritten_without_the_withdrawn_order(session, make_order):
    """Списки по нескольку заказов остались от прежней версии: новые уведомления — по одному заказу."""
    from app.services.subscriptions import retract_withdrawn

    tg = Telegram()
    sub = await _subscribe(session, from_city="Симферополь")
    first = await _fresh_order(make_order, to_city="Ялта")
    second = await _fresh_order(make_order, to_city="Керчь")
    for order in (first, second):  # оба когда-то пришли в одном сообщении №501
        session.add(SubscriptionNotification(subscription_id=sub.id, order_id=order.id,
                                             sent_at=now_utc_naive(), message_id=501))
    await session.commit()
    await _withdraw(session, first)

    await retract_withdrawn(delete=tg.delete, edit=tg.edit)

    assert tg.deleted == []
    (chat, message_id, text), = tg.edited
    assert (chat, message_id) == (1001, 501)
    assert "Керчь" in text and "Ялта" not in text  # осталась только живая заявка
    rows = tg.edit_markups[0]["inline_keyboard"]
    assert len(rows) == 2 and any(b.get("callback_data", "").startswith("take:") for b in rows[0])  # кнопки на месте


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


# --- «Мои фильтры»: несколько фильтров у одного водителя ---------------------------------------------------


async def _my_filters(session):
    session.expire_all()
    return (await session.execute(select(OrderSubscription).order_by(OrderSubscription.id))).scalars().all()


async def test_unchecked_box_applies_the_filter_without_saving_and_keeps_saved_ones(
    client, session, telegram_login_on
):
    await _login(client)
    await client.post("/filter", data={"from_city": "Ялта", "notify": "on"})

    await client.post("/filter", data={"to_city": "Керчь"})  # галочки нет — просто посмотреть ленту

    rows = await _my_filters(session)
    assert len(rows) == 1 and rows[0].params["from_city"] == "Ялта" and rows[0].is_active


async def test_each_filter_with_the_box_is_added_next_to_the_others(client, session, telegram_login_on):
    await _login(client)
    first = await client.post("/filter", data={"from_city": "Ялта", "notify": "on"})
    await client.post("/filter", data={"to_city": "Керчь", "notify": "on"})

    rows = await _my_filters(session)

    assert [r.params["from_city"] or r.params["to_city"] for r in rows] == ["Ялта", "Керчь"]
    assert all(r.is_active for r in rows)
    assert first.status_code == 303
    assert "добавлен в «Мои фильтры»" in (await client.get("/")).text


async def test_the_same_filter_is_not_saved_twice_it_is_switched_back_on(client, session, telegram_login_on):
    await _login(client)
    await client.post("/filter", data={"from_city": "Ялта", "notify": "on"})
    sub_id = (await _my_filters(session))[0].id
    await client.post(f"/filters/{sub_id}/off")

    await client.post("/filter", data={"from_city": "Ялта", "notify": "on"})

    rows = await _my_filters(session)
    assert len(rows) == 1 and rows[0].is_active
    assert "уже есть в «Мои фильтры»" in (await client.get("/")).text


async def test_filter_limit_is_enforced(client, session, telegram_login_on, monkeypatch):
    monkeypatch.setattr(settings, "max_filters_per_driver", 2)
    await _login(client)
    for city in ("Ялта", "Керчь", "Сочи"):
        await client.post("/filter", data={"from_city": city, "notify": "on"})

    assert len(await _my_filters(session)) == 2
    assert "максимум" in (await client.get("/")).text


async def test_filter_form_shows_the_my_filters_button_with_the_count(client, telegram_login_on):
    guest = (await client.get("/")).text
    assert 'method="get" action="/"' in guest and 'name="notify"' not in guest
    assert "Войдите, чтобы получать новые заказы" in guest and "Мои фильтры" not in guest

    await _login(client)
    empty = (await client.get("/")).text
    assert 'method="post" action="/filter"' in empty
    assert 'href="/filters"' in empty and "Мои фильтры (0)" in empty
    assert 'name="notify" checked' not in empty  # галочка по умолчанию снята

    await client.post("/filter", data={"from_city": "Ялта", "notify": "on"})
    await client.post("/filter", data={"to_city": "Керчь", "notify": "on"})
    assert "Мои фильтры (2)" in (await client.get("/")).text


async def test_my_filters_page_lists_filters_with_actions(client, session, telegram_login_on):
    await _login(client)
    empty = (await client.get("/filters")).text
    assert "Пока нет ни одного фильтра" in empty

    await client.post(
        "/filter", data={"from_city": "Краснодар", "from_radius": "50", "to_city": "Сочи", "notify": "on"}
    )
    await client.post("/filter", data={"to_city": "Керчь", "notify": "on"})
    page = (await client.get("/filters")).text

    assert "Мои фильтры (2)" in page
    assert "Откуда: Краснодар (+50 км)" in page and "Куда: Сочи" in page and "Куда: Керчь" in page
    assert page.count("Уведомления включены") == 2
    assert "/?from_city=" in page  # «Открыть в ленте»
    assert "@podacha_bot" in page  # откуда приходят уведомления


async def test_filters_can_be_switched_off_on_and_deleted_one_by_one(client, session, telegram_login_on):
    await _login(client)
    await client.post("/filter", data={"from_city": "Ялта", "notify": "on"})
    await client.post("/filter", data={"to_city": "Керчь", "notify": "on"})
    first, second = await _my_filters(session)

    off = await client.post(f"/filters/{first.id}/off")
    assert off.status_code == 303 and off.headers["location"] == "/filters"
    rows = await _my_filters(session)
    assert [r.is_active for r in rows] == [False, True]  # второй не тронут
    assert "Включить уведомления" in (await client.get("/filters")).text

    await client.post(f"/filters/{first.id}/on")
    assert all(r.is_active for r in await _my_filters(session))

    await client.post(f"/filters/{first.id}/delete")
    rows = await _my_filters(session)
    assert [r.id for r in rows] == [second.id]
    assert "Мои фильтры (1)" in (await client.get("/")).text


async def test_one_driver_cannot_touch_another_drivers_filter(client, session, telegram_login_on):
    await _login(client, id="111")
    await client.post("/filter", data={"from_city": "Ялта", "notify": "on"})
    victim = (await _my_filters(session))[0]
    client.cookies.clear()
    await _login(client, id="222")

    await client.post(f"/filters/{victim.id}/off")
    await client.post(f"/filters/{victim.id}/delete")

    rows = await _my_filters(session)
    assert len(rows) == 1 and rows[0].is_active


async def test_unknown_filter_action_is_404_and_guest_is_sent_to_login(client, telegram_login_on):
    guest = await client.get("/filters")
    assert guest.status_code == 303 and guest.headers["location"].startswith("/login")

    await _login(client)
    assert (await client.post("/filters/1/delete-everything")).status_code == 404


async def test_profile_links_to_my_filters_with_a_summary(client, session, telegram_login_on):
    await _login(client)
    assert "Хотите получать новые заказы в Telegram?" in (await client.get("/profile")).text

    await client.post("/filter", data={"from_city": "Ялта", "notify": "on"})
    await client.post("/filter", data={"to_city": "Керчь", "notify": "on"})
    first = (await _my_filters(session))[0]
    await client.post(f"/filters/{first.id}/off")

    page = (await client.get("/profile")).text

    assert 'href="/filters"' in page and "Мои фильтры (2)" in page
    assert "Сохранено фильтров: 2, с уведомлениями в Telegram: 1" in page


async def test_an_order_matching_two_filters_of_one_driver_is_sent_once(session, make_order):
    await _subscribe(session, from_city="Симферополь")
    await _subscribe(session, to_city="Сочи")  # тот же водитель, второй фильтр
    await _fresh_order(make_order, from_city="Симферополь", to_city="Сочи")
    outbox = Outbox()

    sent = await notify_once(send=outbox)
    again = await notify_once(send=outbox)

    assert sent == 1 and again == 0
    assert len(outbox.messages) == 1


async def test_each_filter_matches_its_own_orders(session, make_order):
    await _subscribe(session, from_city="Симферополь")
    await _subscribe(session, to_city="Керчь")
    await _fresh_order(make_order, from_city="Симферополь", to_city="Сочи")
    await _fresh_order(make_order, from_city="Ялта", to_city="Керчь")
    outbox = Outbox()

    await notify_once(send=outbox)

    texts = " ".join(text for _chat, text in outbox.messages)
    assert "Симферополь → Сочи" in texts and "Ялта → Керчь" in texts
    assert len(outbox.messages) == 2  # по сообщению на фильтр


async def test_switched_off_filter_stays_silent_while_the_other_works(session, make_order):
    off = await _subscribe(session, from_city="Симферополь", active=False)
    await _subscribe(session, to_city="Керчь")
    await _fresh_order(make_order, from_city="Симферополь", to_city="Сочи")
    outbox = Outbox()

    await notify_once(send=outbox)

    assert off.is_active is False and outbox.messages == []
