"""Вход через Telegram-бота: «Старт» в боте → подтверждение → сессия на сайте.

Бота здесь изображает ``FakeApi``: настоящий Telegram не нужен. Главное, что
проверяем, — безопасность: подтвердить вход может только тот, кто нажал «Старт»,
токен работает один раз и не живёт вечно.
"""

import pytest
from sqlalchemy import select

from app.config import settings
from app.models import Driver
from app.web import bot_login
from app.web.bot_login import LoginStore, handle_update

BOT_TOKEN = "123456:test-bot-token"
ALICE = {"id": 111, "first_name": "Алиса", "last_name": "Иванова", "username": "alice", "is_bot": False}
BOB = {"id": 222, "first_name": "Боб", "is_bot": False}


class FakeApi:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    async def __call__(self, method, payload):
        self.calls.append((method, payload))
        return {}

    def sent(self, method):
        return [payload for name, payload in self.calls if name == method]


def _start(user, token=""):
    return {
        "update_id": 1,
        "message": {"from": user, "chat": {"id": user["id"], "type": "private"}, "text": f"/start {token}".strip()},
    }


def _press(user, data):
    return {
        "update_id": 2,
        "callback_query": {
            "id": "cb1",
            "from": user,
            "data": data,
            "message": {"message_id": 7, "chat": {"id": user["id"]}},
        },
    }


@pytest.fixture
def login_on(monkeypatch):
    monkeypatch.setattr(settings, "telegram_login_bot_token", BOT_TOKEN)
    monkeypatch.setattr(settings, "telegram_login_bot_username", "podacha_bot")
    monkeypatch.setattr(settings, "session_secret", "test-session-secret")
    fresh = LoginStore()
    monkeypatch.setattr(bot_login, "store", fresh)
    return fresh


# --- Хранилище ---------------------------------------------------------------


def test_tokens_are_unique_and_fit_start_parameter():
    store = LoginStore()
    first, second = store.create(), store.create()

    assert first.token != second.token
    assert len(first.token) <= 64 and first.token.replace("-", "").replace("_", "").isalnum()


def test_unconfirmed_login_expires():
    store = LoginStore(ttl=60)
    item = store.create(now=1000.0)

    assert store.get(item.token, now=1050.0) is item
    assert store.get(item.token, now=1061.0) is None


def test_store_is_bounded():
    store = LoginStore(max_pending=5)
    for _ in range(50):
        store.create()

    assert len(store._items) <= 5


# --- Диалог с ботом ----------------------------------------------------------


async def test_plain_start_gets_a_welcome_not_a_login():
    api, store = FakeApi(), LoginStore()

    await handle_update(_start(ALICE), api, store)

    assert len(api.sent("sendMessage")) == 1
    assert "Лента заказов" in api.sent("sendMessage")[0]["text"]
    assert not store._items


async def test_start_with_token_asks_for_confirmation():
    api, store = FakeApi(), LoginStore()
    item = store.create("/my")

    await handle_update(_start(ALICE, item.token), api, store)

    message = api.sent("sendMessage")[0]
    buttons = message["reply_markup"]["inline_keyboard"][0]
    assert [b["callback_data"] for b in buttons] == [f"ok:{item.token}", f"no:{item.token}"]
    assert item.state == bot_login.ASKED  # ещё не вошёл — ждём нажатия кнопки
    assert item.user["id"] == 111


async def test_confirm_logs_in_the_same_person():
    api, store = FakeApi(), LoginStore()
    item = store.create()
    await handle_update(_start(ALICE, item.token), api, store)

    await handle_update(_press(ALICE, f"ok:{item.token}"), api, store)

    assert item.state == bot_login.CONFIRMED
    assert api.sent("editMessageText")[0]["text"].startswith("Готово")


async def test_someone_else_cannot_confirm_for_the_person_who_started():
    """Нажать «Войти» за другого нельзя: id берётся из Telegram, а не из текста кнопки."""
    api, store = FakeApi(), LoginStore()
    item = store.create()
    await handle_update(_start(ALICE, item.token), api, store)

    await handle_update(_press(BOB, f"ok:{item.token}"), api, store)

    assert item.state == bot_login.ASKED
    assert store.take_confirmed(item.token) is None


async def test_cancel_discards_the_login():
    api, store = FakeApi(), LoginStore()
    item = store.create()
    await handle_update(_start(ALICE, item.token), api, store)

    await handle_update(_press(ALICE, f"no:{item.token}"), api, store)

    assert store.get(item.token) is None


async def test_confirm_without_start_does_nothing():
    api, store = FakeApi(), LoginStore()
    item = store.create()

    await handle_update(_press(ALICE, f"ok:{item.token}"), api, store)

    assert item.state == bot_login.WAITING


async def test_unknown_or_used_token_is_reported_as_expired():
    api, store = FakeApi(), LoginStore()

    await handle_update(_start(ALICE, "Lнесуществующий"), api, store)

    assert "устарела" in api.sent("sendMessage")[0]["text"]


async def test_bots_and_groups_are_ignored():
    api, store = FakeApi(), LoginStore()
    item = store.create()
    bot = {**ALICE, "is_bot": True}
    await handle_update(_start(bot, item.token), api, store)
    group = _start(ALICE, item.token)
    group["message"]["chat"]["type"] = "group"
    await handle_update(group, api, store)

    assert not api.calls
    assert item.state == bot_login.WAITING


# --- Сайт --------------------------------------------------------------------


async def test_start_endpoint_gives_link_to_the_bot(client, login_on):
    response = await client.post("/auth/bot/start?next=/my")

    data = response.json()
    assert response.status_code == 200
    assert data["url"] == f"https://t.me/podacha_bot?start={data['token']}"
    assert response.headers["cache-control"] == "no-store"
    assert login_on.get(data["token"]).next_url == "/my"


async def test_start_endpoint_refuses_foreign_redirect(client, login_on):
    data = (await client.post("/auth/bot/start?next=https://evil.example")).json()

    assert login_on.get(data["token"]).next_url == "/"


async def test_full_login_creates_driver_and_session(client, login_on, session):
    api = FakeApi()
    data = (await client.post("/auth/bot/start?next=/profile")).json()
    assert (await client.get(f"/auth/bot/status?token={data['token']}")).json() == {"status": "waiting"}

    await handle_update(_start(ALICE, data["token"]), api, login_on)
    assert (await client.get(f"/auth/bot/status?token={data['token']}")).json() == {"status": "asked"}

    await handle_update(_press(ALICE, f"ok:{data['token']}"), api, login_on)
    done = await client.get(f"/auth/bot/status?token={data['token']}")

    assert done.json() == {"status": "done", "next": "/profile"}
    assert "driver_session" in done.cookies
    driver = (await session.execute(select(Driver).where(Driver.telegram_id == 111))).scalar_one()
    assert driver.username == "alice" and driver.first_name == "Алиса"
    # Вошёл по-настоящему: страница входа теперь перекидывает дальше.
    assert (await client.get("/login")).status_code == 303


async def test_confirmed_login_works_only_once(client, login_on):
    api = FakeApi()
    data = (await client.post("/auth/bot/start")).json()
    await handle_update(_start(ALICE, data["token"]), api, login_on)
    await handle_update(_press(ALICE, f"ok:{data['token']}"), api, login_on)

    first = await client.get(f"/auth/bot/status?token={data['token']}")
    client.cookies.clear()
    second = await client.get(f"/auth/bot/status?token={data['token']}")

    assert first.json()["status"] == "done"
    assert second.json() == {"status": "expired"}
    assert "driver_session" not in second.cookies


async def test_status_with_garbage_token_is_expired(client, login_on):
    for token in ("", "x", "L" + "a" * 200):
        assert (await client.get("/auth/bot/status", params={"token": token})).json() == {"status": "expired"}


async def test_endpoints_are_off_when_login_is_off(client, monkeypatch):
    monkeypatch.setattr(settings, "telegram_login_bot_token", "")

    assert (await client.post("/auth/bot/start")).status_code == 404
    assert (await client.get("/auth/bot/status?token=x")).status_code == 404


async def test_login_page_offers_the_bot(client, login_on):
    page = await client.get("/login")

    assert page.status_code == 200
    assert 'id="botLoginLink"' in page.text
    assert "/auth/bot/start" in page.text
    assert 'id="otherAccountHint"' in page.text  # виджет остаётся запасным способом


# --- Кнопка «На сайт» --------------------------------------------------------


async def _confirmed(client, login_on, next_url="/profile"):
    api = FakeApi()
    data = (await client.post(f"/auth/bot/start?next={next_url}")).json()
    await handle_update(_start(ALICE, data["token"]), api, login_on)
    await handle_update(_press(ALICE, f"ok:{data['token']}"), api, login_on)
    return data["token"], api


async def test_done_message_has_site_button_with_one_time_link(client, login_on):
    _, api = await _confirmed(client, login_on)

    button = api.sent("editMessageText")[0]["reply_markup"]["inline_keyboard"][0][0]

    assert button["text"] == "На сайт"
    assert button["url"].startswith(f"{settings.public_base_url.rstrip('/')}/auth/bot/enter?token=E")


async def test_site_button_logs_in_the_browser_that_opens_it(client, login_on, session):
    """Telegram открывает ссылку во встроенном браузере — там и должен появиться вход."""
    _, api = await _confirmed(client, login_on)
    url = api.sent("editMessageText")[0]["reply_markup"]["inline_keyboard"][0][0]["url"]
    token = url.split("token=")[1]

    response = await client.get("/auth/bot/enter", params={"token": token})

    assert response.status_code == 303
    assert response.headers["location"] == "/profile"
    assert "driver_session" in response.cookies
    assert (await session.execute(select(Driver).where(Driver.telegram_id == 111))).scalar_one()


async def test_site_button_works_once(client, login_on):
    _, api = await _confirmed(client, login_on)
    token = api.sent("editMessageText")[0]["reply_markup"]["inline_keyboard"][0][0]["url"].split("token=")[1]

    await client.get("/auth/bot/enter", params={"token": token})
    client.cookies.clear()
    again = await client.get("/auth/bot/enter", params={"token": token})

    assert again.headers["location"] == "/login"
    assert "driver_session" not in again.cookies


async def test_page_and_button_can_both_log_in(client, login_on):
    """Страница входа и кнопка из бота — два браузера одного человека: пускаем оба, по разу."""
    page_token, api = await _confirmed(client, login_on)
    enter = api.sent("editMessageText")[0]["reply_markup"]["inline_keyboard"][0][0]["url"].split("token=")[1]

    polled = await client.get(f"/auth/bot/status?token={page_token}")
    client.cookies.clear()
    entered = await client.get("/auth/bot/enter", params={"token": enter})

    assert polled.json()["status"] == "done"
    assert "driver_session" in entered.cookies
    assert login_on.get(page_token) is None  # оба входа использованы — запись убрана


async def test_site_button_garbage_or_unconfirmed_is_refused(client, login_on):
    data = (await client.post("/auth/bot/start")).json()  # токен есть, но вход не подтверждён

    for token in ("", "E123", data["token"]):
        response = await client.get("/auth/bot/enter", params={"token": token})
        assert response.headers["location"] == "/login"
        assert "driver_session" not in response.cookies
