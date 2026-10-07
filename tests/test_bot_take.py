"""Кнопки под уведомлением бота: «Написать диспетчеру» сразу в чат и «Взять заказ».

Нажатие приходит боту как callback_query; кто нажал, говорит сам Telegram, поэтому берём заказ тем же
атомарным ``take_order``, что и сайт. Bot API здесь изображает ``FakeApi``.
"""

from datetime import timedelta

import pytest
from sqlalchemy import select

from app.config import settings
from app.db.base import SessionLocal
from app.models import Driver, Order, OrderStatus
from app.services import subscriptions as svc
from app.timeutil import now_msk_naive
from app.web import bot_orders
from app.web.bot_login import LoginStore, handle_update

DRIVER_ID = 4001
OTHER_ID = 4002


class FakeApi:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    async def __call__(self, method, payload):
        self.calls.append((method, payload))
        return {}

    def sent(self, method):
        return [payload for name, payload in self.calls if name == method]


@pytest.fixture(autouse=True)
def site(monkeypatch):
    monkeypatch.setattr(settings, "public_base_url", "https://example.test")


async def _register(session, telegram_id=DRIVER_ID):
    session.add(Driver(telegram_id=telegram_id, first_name="Водитель"))
    await session.commit()


async def _order(make_order, **kwargs):
    kwargs.setdefault("pickup_at", now_msk_naive() + timedelta(days=1))
    return await make_order(**kwargs)


def _press(order_id, user_id=DRIVER_ID, buttons=None):
    return {
        "update_id": 1,
        "callback_query": {
            "id": "cb1",
            "from": {"id": user_id, "first_name": "Вод", "is_bot": False},
            "data": f"take:{order_id}",
            "message": {
                "message_id": 77,
                "chat": {"id": user_id},
                "reply_markup": buttons or {"inline_keyboard": [[{"text": "✅", "callback_data": f"take:{order_id}"}]]},
            },
        },
    }


async def _reload(order_id):
    async with SessionLocal() as fresh:
        return (await fresh.execute(select(Order).where(Order.id == order_id))).scalar_one()


# --- Кнопки в уведомлении ----------------------------------------------------------------------------


async def test_chat_button_goes_straight_to_the_dispatcher_with_a_ready_message(make_order):
    order = await _order(make_order, dispatcher_username="mango_disp")

    markup = svc.build_keyboard([order], {})

    first = markup["inline_keyboard"][0][0]
    assert first["text"] == "✉️ Написать диспетчеру"
    assert first["url"].startswith("https://t.me/mango_disp?text=")


async def test_public_group_without_username_gets_the_group_message_button(make_order):
    order = await _order(make_order)
    order.dispatcher_username, order.dispatcher_tg_id = None, None

    markup = svc.build_keyboard([order], {order.source_chat_id: "pubgroup"})

    assert markup["inline_keyboard"][0][0]["url"] == f"https://t.me/pubgroup/{order.source_message_id}"


async def test_no_chat_button_when_it_could_not_open(make_order):
    """Закрытая группа без @username: ссылка по id может быть отвергнута Telegram, на сообщение — не пустят."""
    order = await _order(make_order)
    order.dispatcher_username = None  # есть только tg_id

    take_only = svc.build_keyboard([order], {})["inline_keyboard"][0]

    assert [b["text"] for b in take_only] == ["✅ Взять заказ"]


async def test_digest_has_a_pair_of_buttons_per_order(make_order):
    orders = [await _order(make_order, dispatcher_username=f"d{i}") for i in range(3)]

    rows = svc.build_keyboard(orders, {})["inline_keyboard"]

    assert [[b["text"] for b in row] for row in rows] == [["✉️ 1", "✅ Взять 1"], ["✉️ 2", "✅ Взять 2"], ["✉️ 3", "✅ Взять 3"]]
    assert [row[1]["callback_data"] for row in rows] == [f"take:{o.id}" for o in orders]


async def test_message_text_has_no_order_link(make_order):
    order = await _order(make_order)

    assert "/orders/" not in svc.build_message([order], {})
    assert "/orders/" not in svc.build_message([order, order], {})


# --- Нажатие «Взять заказ» ---------------------------------------------------------------------------------


async def test_pressing_take_assigns_the_order_to_the_driver_and_confirms(session, make_order):
    await _register(session)
    order = await _order(make_order, dispatcher_username="mango_disp")
    api = FakeApi()

    await handle_update(_press(order.id), api, LoginStore())

    taken = await _reload(order.id)
    assert taken.taken_by_token == f"tg:{DRIVER_ID}"
    assert api.sent("answerCallbackQuery")[0]["text"].startswith("Заказ ваш")
    edit = api.sent("editMessageText")[0]  # уведомление об одном заказе превращается в подтверждение
    assert edit["message_id"] == 77 and edit["text"].startswith("✅ Заказ ваш")
    buttons = [b for row in edit["reply_markup"]["inline_keyboard"] for b in row]
    assert any("Написать диспетчеру" in b["text"] for b in buttons)
    assert {"text": "Открыть заказ", "url": f"https://example.test/orders/{order.id}"} in buttons
    assert "+7" not in edit["text"] and "Иван" not in edit["text"]  # контакты клиента в бот не попадают


async def test_taken_order_leaves_the_public_feed(session, make_order, client):
    await _register(session)
    order = await _order(make_order)
    await handle_update(_press(order.id), FakeApi(), LoginStore())

    assert f"/orders/{order.id}" not in (await client.get("/")).text


async def test_second_driver_gets_an_explanation_not_the_order(session, make_order):
    await _register(session)
    await _register(session, OTHER_ID)
    order = await _order(make_order)
    await handle_update(_press(order.id), FakeApi(), LoginStore())
    api = FakeApi()

    await handle_update(_press(order.id, user_id=OTHER_ID), api, LoginStore())

    alert = api.sent("answerCallbackQuery")[0]
    assert alert["show_alert"] is True and "уже взял другой водитель" in alert["text"]
    assert (await _reload(order.id)).taken_by_token == f"tg:{DRIVER_ID}"
    assert api.sent("editMessageText") == []


async def test_pressing_twice_is_not_an_error(session, make_order):
    await _register(session)
    order = await _order(make_order)
    await handle_update(_press(order.id), FakeApi(), LoginStore())
    api = FakeApi()

    await handle_update(_press(order.id), api, LoginStore())

    assert api.sent("answerCallbackQuery")[0]["text"].startswith("Заказ ваш")


async def test_unregistered_user_is_asked_to_sign_in_first(session, make_order):
    order = await _order(make_order)
    api = FakeApi()

    await handle_update(_press(order.id, user_id=999), api, LoginStore())

    assert (await _reload(order.id)).taken_by_token is None
    assert "войдите на сайт" in api.sent("answerCallbackQuery")[0]["text"].lower()


@pytest.mark.parametrize("status", [OrderStatus.CANCELLED, OrderStatus.EXPIRED])
async def test_closed_order_cannot_be_taken(session, make_order, status):
    await _register(session)
    order = await _order(make_order, status=status)
    api = FakeApi()

    await handle_update(_press(order.id), api, LoginStore())

    assert (await _reload(order.id)).taken_by_token is None
    assert "закрыт" in api.sent("answerCallbackQuery")[0]["text"]


async def test_garbage_callback_data_does_not_crash(session):
    api = FakeApi()
    update = _press(1)
    update["callback_query"]["data"] = "take:абвгд"

    await handle_update(update, api, LoginStore())

    assert api.sent("answerCallbackQuery")[0]["show_alert"] is True


async def test_in_a_digest_only_the_taken_row_disappears_and_confirmation_is_a_new_message(session, make_order):
    await _register(session)
    first, second = await _order(make_order), await _order(make_order)
    markup = {
        "inline_keyboard": [
            [{"text": "✉️ 1", "url": "https://t.me/a"}, {"text": "✅ Взять 1", "callback_data": f"take:{first.id}"}],
            [{"text": "✉️ 2", "url": "https://t.me/b"}, {"text": "✅ Взять 2", "callback_data": f"take:{second.id}"}],
        ]
    }
    api = FakeApi()

    await handle_update(_press(first.id, buttons=markup), api, LoginStore())

    left = api.sent("editMessageReplyMarkup")[0]["reply_markup"]["inline_keyboard"]
    assert len(left) == 1 and left[0][1]["callback_data"] == f"take:{second.id}"
    assert api.sent("editMessageText") == []
    assert api.sent("sendMessage")[0]["text"].startswith("✅ Заказ ваш")
    assert (await _reload(second.id)).taken_by_token is None


async def test_login_buttons_still_work_next_to_take(session):
    """«Войти»/«Отмена» разбираются прежним кодом: take-обработчик их не перехватывает."""
    store = LoginStore()
    item = store.create()
    api = FakeApi()
    user = {"id": 5, "first_name": "Алиса", "is_bot": False}
    await handle_update(
        {"update_id": 1, "message": {"from": user, "chat": {"id": 5, "type": "private"}, "text": f"/start {item.token}"}},
        api, store,
    )

    await handle_update(
        {"update_id": 2, "callback_query": {"id": "c", "from": user, "data": f"ok:{item.token}",
                                            "message": {"message_id": 1, "chat": {"id": 5}}}},
        api, store,
    )

    assert item.state == "confirmed"


# --- Запасной вариант, если Telegram отверг кнопку ---------------------------------------------------------------


async def test_rejected_url_button_falls_back_to_take_only(monkeypatch):
    sent = []

    class FakeClient:
        def __init__(self, **kwargs): ...
        async def __aenter__(self): return self
        async def __aexit__(self, *exc): return False

        async def post(self, url, json):
            import httpx

            sent.append(json.get("reply_markup"))
            if len(sent) == 1:
                return httpx.Response(400, text='{"description":"Bad Request: BUTTON_URL_INVALID"}')
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 9}})

    monkeypatch.setattr(svc.httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(settings, "alert_bot_token", "123:abc")
    markup = {"inline_keyboard": [[{"text": "✉️", "url": "https://t.me/x"}, {"text": "✅", "callback_data": "take:1"}]]}

    outcome = await svc.send_message(5, "текст", reply_markup=markup)

    assert outcome == svc.OK and outcome.message_id == 9
    assert sent[1] == {"inline_keyboard": [[{"text": "✅", "callback_data": "take:1"}]]}  # без ссылки, но с «Взять»


async def test_a_real_block_is_still_a_block_and_not_retried_with_other_buttons(monkeypatch):
    calls = []

    class FakeClient:
        def __init__(self, **kwargs): ...
        async def __aenter__(self): return self
        async def __aexit__(self, *exc): return False

        async def post(self, url, json):
            import httpx

            calls.append(url)
            return httpx.Response(403, text='{"description":"Forbidden: bot was blocked by the user"}')

    monkeypatch.setattr(svc.httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(settings, "alert_bot_token", "123:abc")

    assert await svc.send_message(5, "текст", reply_markup={"inline_keyboard": [[{"text": "x", "url": "https://t.me/x"}]]}) == svc.BLOCKED
    assert len(calls) == 1


def test_take_prefix_is_shared_between_keyboard_and_handler(make_order=None):
    assert bot_orders.TAKE_PREFIX == "take:"
