"""Вход через Telegram Login Widget: проверка подписи и гейтинг страниц.

По умолчанию (в .env тестов) TELEGRAM_LOGIN_BOT_TOKEN пуст — вход выключен,
и весь этот файл в основном проверяет, что именно в таком состоянии сайт
ведёт себя ТОЧНО как раньше (анонимная лента без входа). Отдельные тесты
включают вход через monkeypatch и проверяют полный цикл: подпись → сессия →
доступ → выход.
"""

import hashlib
import hmac
import time

import pytest
from sqlalchemy import select

from app.config import settings
from app.models import Driver
from app.web.telegram_login import verify_telegram_login

BOT_TOKEN = "123456:test-bot-token"


def _signed_params(bot_token: str = BOT_TOKEN, **overrides) -> dict:
    fields = {
        "id": "555000111",
        "first_name": "Иван",
        "username": "ivan_driver",
        "auth_date": str(int(time.time())),
    }
    fields.update(overrides)
    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret_key = hashlib.sha256(bot_token.encode("utf-8")).digest()
    fields["hash"] = hmac.new(secret_key, data_check_string.encode("utf-8"), hashlib.sha256).hexdigest()
    return fields


async def test_login_page_explains_how_to_use_another_account(client, monkeypatch):
    """«Войти как …» — память Telegram, а не сайта: страница входа подсказывает, как сменить аккаунт."""
    monkeypatch.setattr(settings, "telegram_login_bot_token", BOT_TOKEN)
    monkeypatch.setattr(settings, "telegram_login_bot_username", "podacha_bot")

    page = await client.get("/login")

    assert page.status_code == 200
    assert 'id="otherAccountHint"' in page.text
    assert "инкогнито" in page.text
    assert "бота" in page.text


# --- Проверка подписи (юнит, без HTTP) ---------------------------------------


def test_valid_signature_is_accepted():
    params = _signed_params()
    assert verify_telegram_login(params, BOT_TOKEN) is True


def test_tampered_field_is_rejected():
    params = _signed_params()
    params["first_name"] = "Пётр"  # подпись считалась для "Иван"
    assert verify_telegram_login(params, BOT_TOKEN) is False


def test_wrong_bot_token_is_rejected():
    params = _signed_params()
    assert verify_telegram_login(params, "999999:другой-бот") is False


def test_missing_hash_is_rejected():
    params = _signed_params()
    del params["hash"]
    assert verify_telegram_login(params, BOT_TOKEN) is False


def test_expired_auth_date_is_rejected():
    old_auth_date = str(int(time.time()) - 2 * 24 * 60 * 60)  # двое суток назад
    params = _signed_params(auth_date=old_auth_date)
    assert verify_telegram_login(params, BOT_TOKEN) is False


def test_empty_bot_token_never_verifies():
    params = _signed_params(bot_token="")
    assert verify_telegram_login(params, "") is False


# --- Гейтинг: пока вход не настроен, поведение как раньше --------------------


async def test_feature_disabled_by_default_in_tests():
    assert settings.telegram_login_enabled is False


async def test_anonymous_can_open_order_detail_when_login_disabled(client, make_order):
    order = await make_order()

    response = await client.get(f"/orders/{order.id}")

    assert response.status_code == 200


async def test_anonymous_can_open_my_orders_when_login_disabled(client):
    response = await client.get("/my")

    assert response.status_code == 200


async def test_login_buttons_hidden_when_feature_disabled(client):
    response = await client.get("/")

    assert 'auth-buttons' not in response.text
    assert 'Зарегистрироваться' not in response.text


# --- С включённым входом ------------------------------------------------------


@pytest.fixture
def telegram_login_on(monkeypatch):
    monkeypatch.setattr(settings, "telegram_login_bot_token", BOT_TOKEN)
    monkeypatch.setattr(settings, "telegram_login_bot_username", "podacha_bot")
    # SESSION_SECRET уже задан в тестовом .env (conftest.py) — без него
    # telegram_login_enabled остался бы False даже с токеном и username.
    assert settings.telegram_login_enabled is True
    yield


async def test_guest_can_open_order_card_without_login(client, make_order, telegram_login_on):
    order = await make_order(client_phone="+79991234567", dispatcher_username="secret_disp")

    response = await client.get(f"/orders/{order.id}")

    assert response.status_code == 200
    # Гость не «хозяин» свободного заказа: телефон клиента скрыт, а ссылка на
    # диспетчера в разметку не попадает (её выдаёт только POST после входа).
    assert "+79991234567" not in response.text
    assert "t.me/secret_disp" not in response.text
    assert "войдите через Telegram" in response.text
    assert ">0<" in response.text.split("Мои заказы", 1)[1][:80]  # у гостя нет «своих»


@pytest.mark.parametrize("action", ["contact", "agree", "complete", "take"])
async def test_guest_action_sends_to_login_and_back_to_the_card(
    client, make_order, telegram_login_on, action
):
    order = await make_order()

    response = await client.post(f"/orders/{order.id}/{action}?back=sort%3Dprice")

    assert response.status_code == 303
    # После входа водитель вернётся на карточку (с параметрами ленты), а не на
    # адрес POST-запроса, который как GET дал бы 405.
    assert response.headers["location"] == (
        f"/login?next=%2Forders%2F{order.id}%3Fback%3Dsort%253Dprice"
    )


async def test_after_login_contact_opens_dispatcher_chat(client, make_order, telegram_login_on):
    order = await make_order(dispatcher_username="real_disp")
    await client.get("/auth/telegram", params=_signed_params())

    response = await client.post(f"/orders/{order.id}/contact")

    assert response.status_code == 303
    assert response.headers["location"].startswith("https://t.me/real_disp?text=")


async def test_anonymous_redirected_to_login_for_my_orders(client, telegram_login_on):
    response = await client.get("/my")

    assert response.status_code == 303
    assert response.headers["location"] == "/login?next=%2Fmy"


async def test_feed_stays_open_without_login(client, make_order, telegram_login_on):
    await make_order(from_city="Сочи")

    response = await client.get("/")

    assert response.status_code == 200
    assert "Сочи" in response.text
    assert "Войти" in response.text
    assert "Зарегистрироваться" in response.text


async def test_full_login_cycle_creates_driver_and_grants_access(client, make_order, session, telegram_login_on):
    order = await make_order()
    params = _signed_params()

    callback = await client.get("/auth/telegram", params=params)

    assert callback.status_code == 303
    assert callback.headers["location"] == "/"
    assert "driver_session" in callback.cookies

    driver = (
        await session.execute(select(Driver).where(Driver.telegram_id == 555000111))
    ).scalar_one()
    assert driver.username == "ivan_driver"
    assert driver.first_name == "Иван"

    # Сессия из cookie реально пускает на закрытые страницы.
    detail = await client.get(f"/orders/{order.id}")
    assert detail.status_code == 200

    my_page = await client.get("/my")
    assert my_page.status_code == 200


async def test_login_preserves_next_redirect_target(client, telegram_login_on):
    params = _signed_params()
    params["next"] = "/my"

    callback = await client.get("/auth/telegram", params=params)

    assert callback.headers["location"] == "/my"


async def test_login_rejects_open_redirect(client, telegram_login_on):
    params = _signed_params()
    params["next"] = "https://evil.example/phishing"

    callback = await client.get("/auth/telegram", params=params)

    # Чужой домен в "next" отбрасывается — уводим на "/", а не наружу.
    assert callback.headers["location"] == "/"


async def test_tampered_login_callback_is_rejected(client, telegram_login_on):
    params = _signed_params()
    params["id"] = "999999999"  # id подменили, подпись считалась для другого

    callback = await client.get("/auth/telegram", params=params)

    assert callback.status_code == 400
    assert "driver_session" not in callback.cookies


async def test_second_login_updates_existing_driver_not_duplicates(client, session, telegram_login_on):
    await client.get("/auth/telegram", params=_signed_params(first_name="Иван"))
    await client.get("/auth/telegram", params=_signed_params(first_name="Иван-обновлённый"))

    drivers = (
        await session.execute(select(Driver).where(Driver.telegram_id == 555000111))
    ).scalars().all()
    assert len(drivers) == 1
    assert drivers[0].first_name == "Иван-обновлённый"


async def test_take_order_uses_stable_telegram_token(client, make_order, session, telegram_login_on):
    """Токен вошедшего водителя — tg:<id>, не случайный UUID: переживает смену
    браузера, потому что это одно и то же значение при каждом входе."""
    from app.models import Order

    order = await make_order()
    order_id = order.id  # до expire_all(): после него это уже lazy-load
    await client.get("/auth/telegram", params=_signed_params())

    await client.post(f"/orders/{order_id}/take")

    session.expire_all()
    reloaded_order = (
        await session.execute(select(Order).where(Order.id == order_id))
    ).scalar_one()
    assert reloaded_order.taken_by_token == "tg:555000111"


async def test_logout_revokes_access(client, make_order, telegram_login_on):
    order = await make_order()
    await client.get("/auth/telegram", params=_signed_params())
    assert (await client.get(f"/orders/{order.id}")).status_code == 200

    await client.post("/auth/logout")

    # Карточка открыта и гостю, а вот действия и «Мои заказы» — снова через вход.
    assert (await client.get(f"/orders/{order.id}")).status_code == 200
    action = await client.post(f"/orders/{order.id}/contact")
    assert action.status_code == 303
    assert action.headers["location"].startswith("/login")
    my_page = await client.get("/my")
    assert my_page.status_code == 303
    assert my_page.headers["location"].startswith("/login")


async def test_driver_stats_count_only_completed_orders_as_earned(
    client, make_order, telegram_login_on
):
    order_a = await make_order(price="1500")
    order_b = await make_order(price="2500")
    await client.get("/auth/telegram", params=_signed_params())

    await client.post(f"/orders/{order_a.id}/agree")
    await client.post(f"/orders/{order_b.id}/agree")

    before = (await client.get("/profile")).text
    assert ">2<" in before  # заказов взял всего (и два в работе)
    assert "0 ₽" in before  # пока ничего не выполнено — заработка нет

    await client.post(f"/orders/{order_b.id}/complete")

    after = (await client.get("/profile")).text
    assert "2500 ₽" in after  # заработано — только по «Выполнен»
    assert "4000" not in after


async def test_guest_profile_redirects_to_login(client, telegram_login_on):
    response = await client.get("/profile")

    assert response.status_code == 303
    assert response.headers["location"] == "/login?next=%2Fprofile"


async def test_profile_is_hidden_when_login_is_not_configured(client):
    assert (await client.get("/profile")).status_code == 404


async def test_profile_shows_driver_stats_recent_orders_and_logout(
    client, make_order, telegram_login_on
):
    done = await make_order(from_city="Ялта", to_city="Керчь", price="9000")
    await client.get(
        "/auth/telegram", params=_signed_params(first_name="Иван", last_name="Петров")
    )
    await client.post(f"/orders/{done.id}/agree")
    await client.post(f"/orders/{done.id}/complete")

    page = (await client.get("/profile")).text

    assert "Иван Петров" in page and "@ivan_driver" in page
    assert "На сайте с" in page
    assert "Ялта → Керчь" in page and f"/orders/{done.id}" in page
    assert "Выполнен" in page and "9000 ₽" in page
    assert 'action="/auth/logout"' in page


async def test_header_shows_profile_instead_of_logout_after_login(
    client, make_order, telegram_login_on
):
    await make_order()
    await client.get("/auth/telegram", params=_signed_params())

    feed = (await client.get("/")).text

    assert 'href="/profile"' in feed
    assert 'class="btn btn-secondary header-logout"' not in feed
    assert 'action="/auth/logout"' not in feed  # выход теперь внутри профиля


async def test_guest_header_has_no_profile_link(client, telegram_login_on):
    feed = (await client.get("/")).text

    assert 'href="/profile"' not in feed
    assert "Войти" in feed
