"""Платная подписка: пробный период, оплата, закрытие функций, плашка, вебхук, напоминания, автопродление."""

from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from app.config import settings
from app.models import Driver, OrderSubscription, Payment
from app.models.payment import PAYMENT_CANCELED, PAYMENT_SUCCEEDED
from app.services import billing, billing_flow
from app.services import payments as payments_module
from app.services.payments.base import STATUS_CANCELED, STATUS_PENDING, STATUS_SUCCEEDED, ProviderPayment
from app.services.subscriptions import BLOCKED, OK, notify_once
from app.timeutil import now_msk_naive, now_utc_naive
from app.web import queries
from tests.test_subscriptions import Outbox, _fresh_order, _subscribe
from tests.test_telegram_login import BOT_TOKEN, _signed_params

NOW = datetime(2026, 11, 1, 12, 0)
USER = "777000111"  # не админ: тестовый админ — 555000111


@pytest.fixture
def subscription_on(monkeypatch):
    monkeypatch.setattr(settings, "subscription_launch_date", "2026-10-20")
    monkeypatch.setattr(settings, "trial_days", 14)
    monkeypatch.setattr(settings, "subscription_price_rub", 1000)
    monkeypatch.setattr(settings, "subscription_period_days", 30)
    monkeypatch.setattr(settings, "public_base_url", "https://example.test")


@pytest.fixture
def telegram_login_on(monkeypatch):
    monkeypatch.setattr(settings, "telegram_login_bot_token", BOT_TOKEN)
    monkeypatch.setattr(settings, "telegram_login_bot_username", "podacha_bot")


def _driver(**fields) -> Driver:
    return Driver(telegram_id=fields.pop("telegram_id", 777000111), **fields)


# --- Логика доступа ----------------------------------------------------------------------------


def test_everyone_has_access_while_subscription_is_off():
    sub = billing.describe(_driver(), now=NOW)
    assert sub.state == billing.STATE_OFF and sub.has_access and not sub.shows_badge


def test_existing_driver_gets_trial_from_launch_date(subscription_on):
    driver = _driver()  # зарегистрирован до запуска: trial_ends_at пуст
    assert billing.describe(driver, now=datetime(2026, 10, 21, 9, 0)).state == billing.STATE_TRIAL
    assert billing.describe(driver, now=datetime(2026, 10, 21, 9, 0)).days == 13  # конец 03.11 00:00 -> 12,6 дня
    expired = billing.describe(driver, now=datetime(2026, 11, 3, 0, 1))
    assert expired.state == billing.STATE_EXPIRED and not expired.has_access


def test_new_driver_trial_starts_at_registration(subscription_on):
    end = billing.new_driver_trial_end(NOW)
    assert end == NOW + timedelta(days=14)
    driver = _driver(trial_ends_at=end)
    assert billing.describe(driver, now=NOW).days == 14
    assert billing.describe(driver, now=NOW + timedelta(days=13, hours=1)).days == 1  # округление вверх


def test_new_driver_has_no_trial_marker_while_subscription_is_off():
    assert billing.new_driver_trial_end(NOW) is None


def test_admin_is_never_limited(subscription_on):
    admin_id = next(iter(settings.admin_ids))
    driver = _driver(telegram_id=admin_id, trial_ends_at=NOW - timedelta(days=100))
    sub = billing.describe(driver, now=NOW)
    assert sub.state == billing.STATE_ADMIN and sub.has_access and sub.shows_badge  # плашка «∞», доступ без ограничений


def test_payment_stacks_on_top_of_the_remaining_trial(subscription_on):
    driver = _driver(trial_ends_at=NOW + timedelta(days=5))

    until = billing.extend(driver, 30, NOW)

    assert until == NOW + timedelta(days=35)  # остаток триала (5 дней) не сгорел
    sub = billing.describe(driver, now=NOW)
    assert sub.state == billing.STATE_PAID and sub.days == 35


def test_payment_after_expiry_starts_from_now(subscription_on):
    driver = _driver(trial_ends_at=NOW - timedelta(days=10))
    assert billing.extend(driver, 30, NOW) == NOW + timedelta(days=30)


# --- Начисление: идемпотентность ---------------------------------------------------------------


async def test_payment_is_credited_once(session, subscription_on):
    session.add(_driver(trial_ends_at=NOW - timedelta(days=1)))
    await session.commit()

    kwargs = dict(telegram_id=777000111, provider="yookassa", provider_payment_id="pay-1", kind="sbp",
                  amount_kop=100000, days=30, now=NOW)
    _, first = await billing.record_success(session, **kwargs)
    _, second = await billing.record_success(session, **kwargs)

    assert (first, second) == (True, False)  # вебхук пришёл дважды — дни не удвоились
    driver = await billing.get_driver(session, 777000111)
    assert driver.paid_until == NOW + timedelta(days=30)
    assert len((await session.execute(select(Payment))).scalars().all()) == 1


async def test_autopay_payment_saves_the_method_for_autorenew(session, subscription_on):
    session.add(_driver())
    await session.commit()

    await billing.record_success(
        session, telegram_id=777000111, provider="yookassa", provider_payment_id="pay-autopay", kind="autopay",
        amount_kop=99000, days=30, payment_method_id="pm-1", now=NOW,
    )

    driver = await billing.get_driver(session, 777000111)
    assert driver.auto_renew is True and driver.payment_method_id == "pm-1"


async def test_one_time_card_payment_does_not_turn_autorenew_on(session, subscription_on):
    session.add(_driver())
    await session.commit()

    await billing.record_success(
        session, telegram_id=777000111, provider="yookassa", provider_payment_id="pay-card", kind="card",
        amount_kop=99000, days=30, payment_method_id="pm-2", now=NOW,
    )

    driver = await billing.get_driver(session, 777000111)
    assert driver.auto_renew is False and driver.payment_method_id is None


async def test_sbp_payment_does_not_turn_autorenew_on(session, subscription_on):
    session.add(_driver())
    await session.commit()

    await billing.record_success(
        session, telegram_id=777000111, provider="yookassa", provider_payment_id="pay-sbp", kind="sbp",
        amount_kop=100000, days=30, payment_method_id="pm-x", now=NOW,
    )

    assert (await billing.get_driver(session, 777000111)).auto_renew is False


async def test_underpaid_or_anonymous_payment_is_not_credited(session, subscription_on):
    session.add(_driver())
    await session.commit()

    cheap = ProviderPayment(id="p-cheap", status=STATUS_SUCCEEDED, amount_kop=100, telegram_id=777000111, kind="sbp", days=30)
    nobody = ProviderPayment(id="p-nobody", status=STATUS_SUCCEEDED, amount_kop=100000, telegram_id=None, kind="sbp", days=30)

    assert (await billing_flow.apply_payment(session, "yookassa", cheap)).credited is False
    assert (await billing_flow.apply_payment(session, "yookassa", nobody)).credited is False
    assert (await billing.get_driver(session, 777000111)).paid_until is None


async def test_grant_and_reset_by_admin(session, subscription_on):
    session.add(_driver(trial_ends_at=NOW - timedelta(days=1)))
    await session.commit()

    until = await billing.grant_days(session, 777000111, 10, now=NOW)
    assert until == NOW + timedelta(days=10)
    assert (await session.execute(select(Payment))).scalars().one().kind == "admin"

    assert await billing.reset_access(session, 777000111, now=NOW) is True
    driver = await billing.get_driver(session, 777000111)
    assert billing.describe(driver, now=NOW + timedelta(seconds=1)).state == billing.STATE_EXPIRED


# --- Плашка и закрытые функции на сайте --------------------------------------------------------


async def _login(client, telegram_id=USER):
    await client.get("/auth/telegram", params=_signed_params(id=telegram_id))


async def _set_driver(session, telegram_id=int(USER), **fields):
    driver = await billing.get_driver(session, telegram_id)
    for name, value in fields.items():
        setattr(driver, name, value)
    await session.commit()


async def test_badge_shows_days_left_to_a_logged_in_driver(client, session, subscription_on, telegram_login_on):
    await _login(client)
    await _set_driver(session, trial_ends_at=now_utc_naive() + timedelta(days=9, hours=3))

    page = await client.get("/")

    assert "Дней подписки: 10" in page.text  # 9 дней и 3 часа — это ещё 10-й день


async def test_badge_is_hidden_from_guests_and_when_subscription_is_off(client, session, telegram_login_on, monkeypatch):
    assert "Дней подписки" not in (await client.get("/")).text
    await _login(client)
    assert "Дней подписки" not in (await client.get("/")).text  # подписка выключена
    monkeypatch.setattr(settings, "subscription_launch_date", "2026-10-20")
    assert "Дней подписки" in (await client.get("/")).text


async def test_expired_driver_sees_the_red_badge_and_a_payment_page(client, session, subscription_on, telegram_login_on):
    await _login(client)
    await _set_driver(session, trial_ends_at=now_utc_naive() - timedelta(days=1))

    assert "Подписка закончилась" in (await client.get("/")).text
    page = await client.get("/subscription")
    assert page.status_code == 200 and "Подписка закончилась" in page.text


async def test_contact_is_closed_after_the_trial_but_open_during_it(
    client, session, make_order, subscription_on, telegram_login_on
):
    order = await make_order(dispatcher_username="real_disp")
    await _login(client)

    await _set_driver(session, trial_ends_at=now_utc_naive() + timedelta(days=3))
    opened = await client.post(f"/orders/{order.id}/contact")
    assert opened.headers["location"].startswith("https://t.me/real_disp")

    await _set_driver(session, trial_ends_at=now_utc_naive() - timedelta(minutes=1))
    closed = await client.post(f"/orders/{order.id}/contact")
    assert closed.status_code == 303 and closed.headers["location"] == "/subscription"


async def test_agree_is_closed_but_complete_and_release_stay_open(
    client, session, make_order, subscription_on, telegram_login_on
):
    order = await make_order(dispatcher_username="real_disp")
    await _login(client)
    await _set_driver(session, trial_ends_at=now_utc_naive() - timedelta(minutes=1))

    agree = await client.post(f"/orders/{order.id}/agree")
    release = await client.post(f"/orders/{order.id}/release")

    assert agree.headers["location"] == "/subscription"
    assert release.headers["location"] != "/subscription"  # возврат своего заказа не закрываем


async def test_paid_driver_can_contact_again(client, session, make_order, subscription_on, telegram_login_on):
    order = await make_order(dispatcher_username="real_disp")
    await _login(client)
    await _set_driver(session, trial_ends_at=now_utc_naive() - timedelta(days=1),
                      paid_until=now_utc_naive() + timedelta(days=20))

    response = await client.post(f"/orders/{order.id}/contact")

    assert response.headers["location"].startswith("https://t.me/real_disp")


async def test_notify_flag_in_filter_needs_a_subscription(client, session, subscription_on, telegram_login_on):
    await _login(client)
    await _set_driver(session, trial_ends_at=now_utc_naive() - timedelta(days=1))

    response = await client.post("/filter", data={"from_city": "Курск", "notify": "on"})

    assert response.status_code == 303
    assert (await session.execute(select(OrderSubscription))).scalars().all() == []


async def test_subscription_page_needs_login(client, subscription_on, telegram_login_on):
    response = await client.get("/subscription")
    assert response.status_code == 303 and response.headers["location"].startswith("/login")


# --- Бот и уведомления -------------------------------------------------------------------------


async def test_notifications_skip_drivers_without_a_subscription(session, make_order, subscription_on, monkeypatch):
    monkeypatch.setattr(settings, "public_base_url", "https://example.test")
    session.add(Driver(telegram_id=2001, trial_ends_at=now_utc_naive() - timedelta(days=1)))
    session.add(Driver(telegram_id=2002, trial_ends_at=now_utc_naive() + timedelta(days=5)))
    await session.commit()
    await _subscribe(session, telegram_id=2001, from_city="Курск")
    await _subscribe(session, telegram_id=2002, from_city="Курск")
    await _fresh_order(make_order, from_city="Курск", to_city="Тольятти")
    box = Outbox()

    await notify_once(send=box)

    assert {chat for chat, _ in box.messages} == {2002}  # без подписки (2001) уведомление не ушло


async def test_bot_take_is_closed_without_a_subscription(session, make_order, subscription_on, monkeypatch):
    from app.web.bot_login import LoginStore, handle_update
    from tests.test_bot_take import FakeApi, _order, _press

    monkeypatch.setattr(settings, "public_base_url", "https://example.test")
    session.add(Driver(telegram_id=4001, first_name="В", trial_ends_at=now_utc_naive() - timedelta(days=1)))
    await session.commit()
    order = await _order(make_order)
    api = FakeApi()

    await handle_update(_press(order.id), api, LoginStore())

    alert = api.sent("answerCallbackQuery")[0]
    assert "Подписка закончилась" in alert["text"] and alert["show_alert"] is True
    await session.refresh(order)
    assert order.taken_by_token is None  # заказ не взят


# --- Оплата: провайдер, вебхук, возврат --------------------------------------------------------


class FakeProvider:
    name = "fake"

    def __init__(self):
        self.created: list[dict] = []
        self.payments: dict[str, ProviderPayment] = {}
        self.charges: list[dict] = []
        self.charge_status = STATUS_SUCCEEDED
        self.refunds: dict = {}

    async def create_payment(self, *, telegram_id, kind, amount_kop, days, return_url, description):
        payment_id = f"fake-{len(self.created) + 1}"
        info = ProviderPayment(id=payment_id, status=STATUS_PENDING, amount_kop=amount_kop, telegram_id=telegram_id,
                               kind=kind, days=days, confirmation_url=f"https://pay.test/{payment_id}")
        self.created.append({"kind": kind, "amount_kop": amount_kop, "return_url": return_url})
        self.payments[payment_id] = info
        return info

    async def charge_saved(self, *, telegram_id, payment_method_id, amount_kop, days, description, idempotence_key):
        self.charges.append({"telegram_id": telegram_id, "method": payment_method_id, "key": idempotence_key})
        info = ProviderPayment(id=f"auto-{len(self.charges)}", status=self.charge_status, amount_kop=amount_kop,
                               telegram_id=telegram_id, kind="autorenew", days=days)
        self.payments[info.id] = info
        return info

    async def fetch(self, payment_id):
        from app.services.payments import PaymentError

        if payment_id not in self.payments:
            raise PaymentError("нет такого платежа", status=404)
        return self.payments[payment_id]

    async def fetch_refund(self, refund_id):
        from app.services.payments import PaymentError

        if refund_id not in self.refunds:
            raise PaymentError("нет такого возврата", status=404)
        return self.refunds[refund_id]

    def refund(self, refund_id, payment_id, amount_kop, status=STATUS_SUCCEEDED):
        from app.services.payments.base import ProviderRefund

        self.refunds[refund_id] = ProviderRefund(id=refund_id, status=status, payment_id=payment_id, amount_kop=amount_kop)

    def succeed(self, payment_id, method_id=None):
        old = self.payments[payment_id]
        self.payments[payment_id] = ProviderPayment(
            id=old.id, status=STATUS_SUCCEEDED, amount_kop=old.amount_kop, telegram_id=old.telegram_id,
            kind=old.kind, days=old.days, payment_method_id=method_id,
        )


@pytest.fixture
def provider(monkeypatch):
    fake = FakeProvider()
    monkeypatch.setattr(payments_module, "get_provider", lambda: fake)
    monkeypatch.setattr("app.web.billing_routes.get_provider", lambda: fake)
    monkeypatch.setattr("app.services.billing_flow.settings.public_base_url", "https://example.test")
    return fake


@pytest.fixture
def sent_messages(monkeypatch):
    sent: list[tuple[int, str]] = []

    async def fake_send(chat_id, text, reply_markup=None):
        sent.append((chat_id, text))
        return OK

    monkeypatch.setattr("app.services.subscriptions.send_message", fake_send)
    return sent


async def test_pay_button_creates_a_payment_and_redirects_to_the_provider(
    client, session, subscription_on, telegram_login_on, provider
):
    await _login(client)

    response = await client.post("/subscription/pay", data={"kind": "sbp"})

    assert response.status_code == 303 and response.headers["location"] == "https://pay.test/fake-1"
    assert provider.created[0]["kind"] == "sbp" and provider.created[0]["amount_kop"] == 100000
    stored = (await session.execute(select(Payment))).scalars().one()
    assert stored.status == "pending" and stored.days == 30


async def test_card_button_is_a_one_time_payment_and_autopay_link_saves_the_card(
    client, subscription_on, telegram_login_on, provider
):
    await _login(client)
    await client.post("/subscription/pay", data={"kind": "card"})
    await client.post("/subscription/pay", data={"kind": "autopay"})
    await client.post("/subscription/pay", data={"kind": "garbage"})  # неизвестный способ — обычная СБП

    assert [c["kind"] for c in provider.created] == ["card", "autopay", "sbp"]


async def test_webhook_credits_the_payment_once_and_notifies(
    client, session, subscription_on, telegram_login_on, provider, sent_messages
):
    await _login(client)
    await client.post("/subscription/pay", data={"kind": "sbp"})
    provider.succeed("fake-1")

    first = await client.post("/payments/yookassa", json={"event": "payment.succeeded", "object": {"id": "fake-1"}})
    second = await client.post("/payments/yookassa", json={"event": "payment.succeeded", "object": {"id": "fake-1"}})

    assert first.status_code == 200 and second.status_code == 200
    driver = await billing.get_driver(session, int(USER))
    await session.refresh(driver)
    assert driver.paid_until is not None
    assert (await session.execute(select(Payment))).scalars().one().status == PAYMENT_SUCCEEDED
    assert len(sent_messages) == 1 and "Подписка продлена" in sent_messages[0][1]  # повтор не шлёт второе сообщение


async def test_webhook_does_not_trust_the_body_status(
    client, session, subscription_on, telegram_login_on, provider, sent_messages
):
    await _login(client)
    await client.post("/subscription/pay", data={"kind": "sbp"})  # у провайдера платёж всё ещё pending

    await client.post("/payments/yookassa", json={"event": "payment.succeeded", "object": {"id": "fake-1", "status": "succeeded"}})

    driver = await billing.get_driver(session, int(USER))
    assert driver.paid_until is None and sent_messages == []


async def test_webhook_ignores_garbage(client, subscription_on, telegram_login_on, provider):
    assert (await client.post("/payments/yookassa", content=b"not json")).status_code == 200
    assert (await client.post("/payments/yookassa", json={"object": {}})).status_code == 200


async def test_return_page_settles_a_pending_payment_even_without_a_webhook(
    client, session, subscription_on, telegram_login_on, provider, sent_messages
):
    await _login(client)
    await client.post("/subscription/pay", data={"kind": "sbp"})
    provider.succeed("fake-1")

    response = await client.get("/subscription/return")

    assert response.status_code == 303 and response.headers["location"] == "/subscription"
    driver = await billing.get_driver(session, int(USER))
    await session.refresh(driver)
    assert driver.paid_until is not None


async def test_autorenew_can_be_turned_off(client, session, subscription_on, telegram_login_on):
    await _login(client)
    await _set_driver(session, auto_renew=True, payment_method_id="pm-1")

    await client.post("/subscription/autorenew/off")

    driver = await billing.get_driver(session, int(USER))
    await session.refresh(driver)
    assert driver.auto_renew is False and driver.payment_method_id is None


# --- Напоминания и автопродление ---------------------------------------------------------------


class Messages:
    def __init__(self, outcome=OK):
        self.outcome = outcome
        self.items: list[tuple[int, str]] = []

    async def __call__(self, chat_id, text, reply_markup=None):
        self.items.append((chat_id, text))
        return self.outcome


async def test_reminder_three_days_before_the_end_is_sent_once(session, subscription_on):
    session.add(Driver(telegram_id=3001, trial_ends_at=NOW + timedelta(days=2, hours=5)))
    session.add(Driver(telegram_id=3002, trial_ends_at=NOW + timedelta(days=20)))
    await session.commit()
    box = Messages()

    first = await billing_flow.remind_expiring(send=box, now=NOW)
    second = await billing_flow.remind_expiring(send=box, now=NOW + timedelta(hours=1))

    assert (first, second) == (1, 0)
    assert [chat for chat, _ in box.items] == [3001] and "заканчивается" in box.items[0][1]


async def test_expired_notice_is_sent_once_and_then_not_forever(session, subscription_on):
    session.add(Driver(telegram_id=3003, trial_ends_at=NOW - timedelta(hours=2)))
    session.add(Driver(telegram_id=3004, trial_ends_at=NOW - timedelta(days=30)))
    await session.commit()
    box = Messages()

    await billing_flow.remind_expiring(send=box, now=NOW)
    await billing_flow.remind_expiring(send=box, now=NOW + timedelta(hours=3))

    assert [chat for chat, _ in box.items] == [3003]  # давно закончившимся (3004) не пишем
    assert "закончилась" in box.items[0][1]


async def test_blocked_bot_is_not_retried_forever(session, subscription_on):
    session.add(Driver(telegram_id=3005, trial_ends_at=NOW + timedelta(days=1)))
    await session.commit()
    box = Messages(outcome=BLOCKED)

    await billing_flow.remind_expiring(send=box, now=NOW)
    await billing_flow.remind_expiring(send=box, now=NOW + timedelta(hours=1))

    assert len(box.items) == 1


async def test_no_reminders_while_subscription_is_off(session):
    session.add(Driver(telegram_id=3006, trial_ends_at=NOW + timedelta(days=1)))
    await session.commit()
    assert await billing_flow.remind_expiring(send=Messages(), now=NOW) == 0


async def test_autorenew_charges_the_saved_card_a_day_before_the_end(session, subscription_on, provider):
    session.add(Driver(telegram_id=3010, trial_ends_at=NOW - timedelta(days=5), paid_until=NOW + timedelta(hours=10),
                       auto_renew=True, payment_method_id="pm-9"))
    session.add(Driver(telegram_id=3011, trial_ends_at=NOW - timedelta(days=5), paid_until=NOW + timedelta(days=10),
                       auto_renew=True, payment_method_id="pm-10"))  # ещё рано
    await session.commit()
    box = Messages()

    charged = await billing_flow.autorenew_due(provider=provider, send=box, now=NOW)
    again = await billing_flow.autorenew_due(provider=provider, send=box, now=NOW + timedelta(minutes=1))

    assert (charged, again) == (1, 0)  # второй проход не списывает повторно
    assert [c["telegram_id"] for c in provider.charges] == [3010]
    driver = await billing.get_driver(session, 3010)
    await session.refresh(driver)
    assert driver.paid_until == NOW + timedelta(hours=10) + timedelta(days=30)
    assert "продлена" in box.items[0][1]


async def test_failed_autorenew_notifies_and_stops_after_three_failures(session, subscription_on, provider):
    provider.charge_status = STATUS_CANCELED
    session.add(Driver(telegram_id=3020, trial_ends_at=NOW - timedelta(days=5), paid_until=NOW + timedelta(hours=5),
                       auto_renew=True, payment_method_id="pm-bad"))
    await session.commit()
    box = Messages()

    for attempt in range(3):
        await billing_flow.autorenew_due(provider=provider, send=box, now=NOW + timedelta(hours=21 * attempt))

    driver = await billing.get_driver(session, 3020)
    await session.refresh(driver)
    assert driver.renew_failures == 3 and driver.auto_renew is False
    assert all("Не удалось списать" in text for _, text in box.items)
    statuses = {p.status for p in (await session.execute(select(Payment))).scalars().all()}
    assert statuses == {PAYMENT_CANCELED}


# --- Регистрация и админка ---------------------------------------------------------------------


async def test_new_driver_gets_a_trial_marker_when_subscription_is_on(session, subscription_on):
    driver = await queries.upsert_driver(session, {"id": "9100", "first_name": "Новый"})
    assert driver.trial_ends_at is not None
    assert driver.trial_ends_at - now_utc_naive() > timedelta(days=13)


async def test_new_driver_has_no_marker_when_subscription_is_off(session):
    driver = await queries.upsert_driver(session, {"id": "9101", "first_name": "Новый"})
    assert driver.trial_ends_at is None


async def test_admin_can_grant_days_and_reset_from_the_user_card(client, session, subscription_on, telegram_login_on):
    session.add(Driver(telegram_id=8800, first_name="Клиент", trial_ends_at=now_utc_naive() - timedelta(days=1)))
    await session.commit()
    await client.get("/auth/telegram", params=_signed_params())  # тестовый админ

    page = await client.get("/admin/users/8800")
    assert "Подписка" in page.text and "закончилась" in page.text

    await client.post("/admin/users/8800/subscription/grant", data={"days": "15"})
    driver = await billing.get_driver(session, 8800)
    await session.refresh(driver)
    assert driver.paid_until is not None
    assert billing.describe(driver, now=now_utc_naive()).state == billing.STATE_PAID

    await client.post("/admin/users/8800/subscription/reset")
    await session.refresh(driver)
    assert driver.paid_until is None


async def test_non_admin_cannot_use_admin_subscription_routes(client, session, subscription_on, telegram_login_on):
    session.add(Driver(telegram_id=8801, first_name="Клиент"))
    await session.commit()
    await _login(client)

    response = await client.post("/admin/users/8801/subscription/grant", data={"days": "15"})

    assert response.status_code == 404
    assert (await billing.get_driver(session, 8801)).paid_until is None


async def test_header_counts_carries_the_subscription_for_logged_in_driver(session, subscription_on):
    session.add(Driver(telegram_id=9200, trial_ends_at=now_utc_naive() + timedelta(days=4)))
    await session.commit()

    counts = await queries.header_counts(session, "tg:9200")

    assert counts["subscription"].state == billing.STATE_TRIAL and counts["subscription"].days == 4
    assert (await queries.header_counts(session, ""))["subscription"].state == billing.STATE_OFF


def test_yookassa_payload_parsing():
    from app.services.payments.yookassa import parse_payment

    info = parse_payment({
        "id": "p1", "status": "succeeded", "amount": {"value": "1000.00", "currency": "RUB"},
        "metadata": {"telegram_id": "42", "kind": "card", "days": "30"},
        "payment_method": {"id": "pm-1", "saved": True},
    })

    assert (info.id, info.status, info.amount_kop, info.telegram_id, info.kind, info.days, info.payment_method_id) == (
        "p1", STATUS_SUCCEEDED, 100000, 42, "card", 30, "pm-1",
    )
    unsaved = parse_payment({"id": "p2", "status": "pending", "payment_method": {"id": "pm-2", "saved": False},
                             "confirmation": {"confirmation_url": "https://pay"}})
    assert unsaved.payment_method_id is None and unsaved.status == STATUS_PENDING and unsaved.confirmation_url == "https://pay"


def test_now_helpers_are_naive_utc():
    assert now_utc_naive().tzinfo is None and now_msk_naive().tzinfo is None


async def test_admin_sees_an_infinity_badge_and_is_not_limited(client, subscription_on, telegram_login_on):
    await client.get("/auth/telegram", params=_signed_params())  # тестовый админ

    page = await client.get("/")

    assert "Дней подписки: ∞" in page.text


async def test_admin_can_open_payment_and_pay_for_a_check(client, session, subscription_on, telegram_login_on, provider):
    await client.get("/auth/telegram", params=_signed_params())  # тестовый админ

    page = await client.get("/subscription")
    assert "Оплатить по СБП" in page.text  # у админа кнопки оплаты для проверки

    pay = await client.post("/subscription/pay", data={"kind": "sbp"})
    assert pay.status_code == 303 and pay.headers["location"] == "https://pay.test/fake-1"


# --- Страница по макету: состояния и баннеры ----------------------------------------------------


async def test_trial_page_shows_days_benefits_and_both_pay_buttons(
    client, session, subscription_on, telegram_login_on, provider
):
    await _login(client)
    await _set_driver(session, trial_ends_at=now_utc_naive() + timedelta(days=8, hours=5))

    page = (await client.get("/subscription")).text

    assert "Пробный период" in page and ">9<" in page  # крупное число дней
    assert "Что даёт подписка" in page and "Платежей пока не было." in page
    assert "Оплатить по СБП" in page and "Оплатить банковской картой" in page
    assert page.rindex("Настроить автоплатёж") > page.rindex("Оплатить банковской картой")  # автоплатёж в конце, мелко
    assert page.index("Оплатить банковской картой") < page.index("Другой способ оплаты") < page.rindex("Настроить автоплатёж")


async def test_expired_page_lists_what_is_closed_and_what_stays_open(
    client, session, subscription_on, telegram_login_on, provider
):
    await _login(client)
    await _set_driver(session, trial_ends_at=now_utc_naive() - timedelta(days=2))

    page = (await client.get("/subscription")).text

    assert "Доступ ограничен" in page and "Закрыто" in page and "Остаётся открытым" in page
    assert "Что даёт подписка" not in page


async def test_paid_page_with_autorenew_hides_the_card_button(
    client, session, subscription_on, telegram_login_on, provider
):
    await _login(client)
    await _set_driver(session, trial_ends_at=now_utc_naive() - timedelta(days=9),
                      paid_until=now_utc_naive() + timedelta(days=33, hours=1),
                      auto_renew=True, payment_method_id="pm-1")

    page = (await client.get("/subscription")).text

    assert "Подписка оплачена" in page and "Автопродление включено" in page and "Отключить автопродление" in page
    assert "Настроить автоплатёж" not in page  # уже настроен
    assert "Оплатить по СБП" in page and "Оплатить банковской картой" in page
    assert "Дни добавятся к остатку: будет 64" in page


async def test_error_banner_with_retry_when_provider_fails(
    client, session, subscription_on, telegram_login_on, provider, monkeypatch
):
    from app.services.payments import PaymentError

    async def broken(**kwargs):
        raise PaymentError("down")

    monkeypatch.setattr(provider, "create_payment", broken)
    await _login(client)

    redirect = await client.post("/subscription/pay", data={"kind": "card"})
    page = (await client.get("/subscription")).text

    assert redirect.headers["location"] == "/subscription"
    assert "Не удалось создать платёж" in page and "Деньги не списаны" in page and "Повторить" in page
    assert 'name="kind" value="card"' in page  # «Повторить» повторяет тот же способ
    assert "Не удалось создать платёж" not in (await client.get("/subscription")).text  # сообщение одноразовое


async def test_success_banner_after_return_from_payment(
    client, session, subscription_on, telegram_login_on, provider, sent_messages
):
    await _login(client)
    await client.post("/subscription/pay", data={"kind": "sbp"})
    provider.succeed("fake-1")

    await client.get("/subscription/return")
    page = (await client.get("/subscription")).text

    assert "Оплата получена" in page and "Подписка продлена до" in page
    assert "Подписка оплачена" in page and "Оплачен" in page  # и в истории платежей


async def test_any_method_button_creates_a_payment_without_forcing_the_method(
    client, session, subscription_on, telegram_login_on, provider
):
    await _login(client)

    response = await client.post("/subscription/pay", data={"kind": "any"})

    assert response.headers["location"] == "https://pay.test/fake-1"
    assert provider.created[0]["kind"] == "any"
    assert (await session.execute(select(Payment))).scalars().one().kind == "any"


async def test_any_method_payment_does_not_turn_autorenew_on(session, subscription_on):
    session.add(_driver())
    await session.commit()

    await billing.record_success(
        session, telegram_id=777000111, provider="yookassa", provider_payment_id="pay-any", kind="any",
        amount_kop=99000, days=30, payment_method_id="pm-z", now=NOW,
    )

    assert (await billing.get_driver(session, 777000111)).auto_renew is False


def test_yookassa_any_kind_leaves_the_method_unset(monkeypatch):
    import asyncio

    from app.services.payments.yookassa import YooKassa

    sent = {}

    async def fake_request(self, method, url, *, json=None, idempotence_key=None):
        sent.update(json)
        return {"id": "p", "status": "pending", "amount": {"value": "990.00"}, "confirmation": {"confirmation_url": "u"}}

    monkeypatch.setattr(YooKassa, "_request", fake_request)
    provider = YooKassa("1", "k")
    for kind in ("sbp", "card", "autopay", "any"):
        sent.clear()
        asyncio.run(provider.create_payment(telegram_id=1, kind=kind, amount_kop=99000, days=30, return_url="r", description="d"))
        if kind == "any":
            assert "payment_method_data" not in sent and "save_payment_method" not in sent
        elif kind == "autopay":
            assert sent["payment_method_data"] == {"type": "bank_card"} and sent["save_payment_method"] is True
        elif kind == "card":
            assert sent["payment_method_data"] == {"type": "bank_card"} and "save_payment_method" not in sent
        else:
            assert sent["payment_method_data"] == {"type": "sbp"}


async def test_offer_page_is_a_complete_document_without_a_template_remark(client, monkeypatch, subscription_on):
    monkeypatch.setattr(settings, "offer_seller_name", "Иванов Иван Иванович")
    monkeypatch.setattr(settings, "offer_seller_inn", "123456789012")
    monkeypatch.setattr(settings, "subscription_price_rub", 990)

    page = await client.get("/offer")
    text = page.text

    assert page.status_code == 200
    assert "Публичная оферта" in text and "Редакция от" in text
    assert "Иванов Иван Иванович" in text and "ИНН 123456789012" in text
    assert "990 ₽ за 30 дней" in text and "14 дней" in text
    for section in ("Предмет договора", "Пробный период", "Автоплатёж", "возврат", "Персональные данные", "Реквизиты"):
        assert section in text
    assert "шаблон" not in text.lower() and "сверьте" not in text.lower()


async def test_webhook_ignores_non_payment_events_without_asking_the_provider(
    client, subscription_on, telegram_login_on, provider
):
    # привязка способа оплаты и возврат приходят с id, который не является платежом
    for event in ("payment_method.active", "refund.succeeded"):
        response = await client.post("/payments/yookassa", json={"event": event, "object": {"id": "not-a-payment"}})
        assert response.status_code == 200, event  # не 502: иначе ЮKassa будет повторять уведомление


# --- Возвраты: дни списываются сами ---------------------------------------------------------------


async def _paid_driver(session, payment_id="pay-r", days=30, amount_kop=99000):
    session.add(_driver(trial_ends_at=NOW - timedelta(days=1)))
    await session.commit()
    await billing.record_success(
        session, telegram_id=777000111, provider="yookassa", provider_payment_id=payment_id, kind="sbp",
        amount_kop=amount_kop, days=days, now=NOW,
    )
    return await billing.get_driver(session, 777000111)


async def test_full_refund_takes_back_all_the_days(session, subscription_on):
    driver = await _paid_driver(session)
    assert driver.paid_until == NOW + timedelta(days=30)

    refund, days = await billing.record_refund(
        session, provider_refund_id="ref-1", provider_payment_id="pay-r", amount_kop=99000,
    )

    await session.refresh(driver)
    assert days == 30 and refund.days == 30
    assert driver.paid_until == NOW  # вернули все деньги — вернули и все дни
    payment = (await session.execute(select(Payment))).scalars().one()
    assert payment.status == "refunded"
    assert billing.describe(driver, now=NOW + timedelta(seconds=1)).state == billing.STATE_EXPIRED


async def test_repeated_refund_notification_does_not_take_days_twice(session, subscription_on):
    driver = await _paid_driver(session)
    kwargs = dict(provider_refund_id="ref-1", provider_payment_id="pay-r", amount_kop=99000)

    _, first = await billing.record_refund(session, **kwargs)
    _, second = await billing.record_refund(session, **kwargs)

    await session.refresh(driver)
    assert (first, second) == (30, 0) and driver.paid_until == NOW


async def test_partial_refunds_take_days_proportionally_and_finish_exactly(session, subscription_on):
    driver = await _paid_driver(session)

    _, half = await billing.record_refund(session, provider_refund_id="r1", provider_payment_id="pay-r", amount_kop=49500)
    await session.refresh(driver)
    payment = (await session.execute(select(Payment))).scalars().one()
    assert half == 15 and driver.paid_until == NOW + timedelta(days=15) and payment.status == "succeeded"

    _, rest = await billing.record_refund(session, provider_refund_id="r2", provider_payment_id="pay-r", amount_kop=49500)
    await session.refresh(driver)
    await session.refresh(payment)
    assert rest == 15 and driver.paid_until == NOW and payment.status == "refunded"  # ровно, без остатка и лишнего


async def test_refund_keeps_the_rest_of_the_trial(session, subscription_on):
    session.add(_driver(trial_ends_at=NOW + timedelta(days=5)))
    await session.commit()
    await billing.record_success(
        session, telegram_id=777000111, provider="yookassa", provider_payment_id="pay-t", kind="sbp",
        amount_kop=99000, days=30, now=NOW,
    )

    await billing.record_refund(session, provider_refund_id="rt", provider_payment_id="pay-t", amount_kop=99000)

    driver = await billing.get_driver(session, 777000111)
    sub = billing.describe(driver, now=NOW)
    assert sub.state == billing.STATE_TRIAL and sub.days == 5  # остаток пробного периода не пострадал


async def test_refund_of_an_unknown_or_unpaid_payment_is_ignored(session, subscription_on):
    driver = await _paid_driver(session)

    refund, days = await billing.record_refund(
        session, provider_refund_id="ref-x", provider_payment_id="no-such-payment", amount_kop=99000,
    )

    await session.refresh(driver)
    assert (refund, days) == (None, 0) and driver.paid_until == NOW + timedelta(days=30)


async def test_refund_webhook_takes_the_days_notifies_and_marks_history(
    client, session, subscription_on, telegram_login_on, provider, sent_messages
):
    await _login(client)
    await client.post("/subscription/pay", data={"kind": "sbp"})
    provider.succeed("fake-1")
    await client.post("/payments/yookassa", json={"event": "payment.succeeded", "object": {"id": "fake-1"}})
    sent_messages.clear()
    provider.refund("ref-9", "fake-1", 100000)

    response = await client.post("/payments/yookassa", json={"event": "refund.succeeded", "object": {"id": "ref-9"}})
    repeat = await client.post("/payments/yookassa", json={"event": "refund.succeeded", "object": {"id": "ref-9"}})

    assert response.status_code == 200 and repeat.status_code == 200
    driver = await billing.get_driver(session, int(USER))
    await session.refresh(driver)
    assert driver.paid_until <= now_utc_naive() + timedelta(days=14)  # осталось только то, что было до оплаты
    assert len(sent_messages) == 1 and "Платёж возвращён" in sent_messages[0][1]  # повтор не шлёт второго сообщения
    assert "Возвращён" in (await client.get("/subscription")).text


async def test_refund_webhook_trusts_the_provider_not_the_body(
    client, session, subscription_on, telegram_login_on, provider, sent_messages
):
    await _login(client)
    await client.post("/subscription/pay", data={"kind": "sbp"})
    provider.succeed("fake-1")
    await client.post("/payments/yookassa", json={"event": "payment.succeeded", "object": {"id": "fake-1"}})
    provider.refund("ref-p", "fake-1", 100000, status="pending")  # у провайдера возврат ещё не прошёл

    await client.post("/payments/yookassa", json={"event": "refund.succeeded", "object": {"id": "ref-p", "status": "succeeded"}})

    driver = await billing.get_driver(session, int(USER))
    await session.refresh(driver)
    assert driver.paid_until > now_utc_naive() + timedelta(days=20)  # дни на месте


def test_yookassa_refund_payload_parsing():
    from app.services.payments.yookassa import parse_refund

    info = parse_refund({"id": "r1", "status": "succeeded", "payment_id": "p1", "amount": {"value": "990.00", "currency": "RUB"}})

    assert (info.id, info.status, info.payment_id, info.amount_kop) == ("r1", STATUS_SUCCEEDED, "p1", 99000)


async def test_webhook_for_an_unknown_payment_or_refund_is_not_retried(client, subscription_on, telegram_login_on, provider):
    # у магазина нет такого платежа/возврата (4xx): отвечаем 200, чтобы ЮKassa не повторяла уведомление
    for event in ("payment.succeeded", "refund.succeeded"):
        response = await client.post("/payments/yookassa", json={"event": event, "object": {"id": "ghost"}})
        assert response.status_code == 200, event


async def test_webhook_asks_to_retry_on_provider_outage(client, subscription_on, telegram_login_on, provider, monkeypatch):
    from app.services.payments import PaymentError

    async def down(payment_id):
        raise PaymentError("сеть", status=None)

    monkeypatch.setattr(provider, "fetch", down)

    response = await client.post("/payments/yookassa", json={"event": "payment.succeeded", "object": {"id": "p"}})

    assert response.status_code == 502  # временный сбой — пусть ЮKassa повторит позже


# --- Ожидание оплаты истекает через 10 минут ------------------------------------------------------


async def _backdate(session, minutes):
    from sqlalchemy import update

    await session.execute(update(Payment).values(created_at=now_utc_naive() - timedelta(minutes=minutes)))
    await session.commit()


async def test_unpaid_payment_stops_waiting_after_ten_minutes(
    client, session, subscription_on, telegram_login_on, provider
):
    await _login(client)
    await client.post("/subscription/pay", data={"kind": "sbp"})

    await _backdate(session, 5)
    fresh = (await client.get("/subscription")).text
    await _backdate(session, 11)
    stale = (await client.get("/subscription")).text

    assert "Ожидает оплаты" in fresh  # пять минут — ещё ждём
    assert "Ожидает оплаты" not in stale and "Отменён" in stale  # одиннадцать — ожидание спало
    assert (await session.execute(select(Payment))).scalars().one().status == PAYMENT_CANCELED


async def test_payment_made_after_the_wait_expired_is_still_credited(
    client, session, subscription_on, telegram_login_on, provider, sent_messages
):
    await _login(client)
    await client.post("/subscription/pay", data={"kind": "sbp"})
    await _backdate(session, 15)
    await client.get("/subscription")  # ожидание спало: платёж помечен неоплаченным
    provider.succeed("fake-1")  # а водитель всё же оплатил

    await client.post("/payments/yookassa", json={"event": "payment.succeeded", "object": {"id": "fake-1"}})

    driver = await billing.get_driver(session, int(USER))
    await session.refresh(driver)
    assert driver.paid_until is not None  # дни начислены
    assert (await session.execute(select(Payment))).scalars().one().status == PAYMENT_SUCCEEDED


async def test_reconcile_settles_paid_and_expires_unpaid(session, subscription_on, provider, sent_messages):
    session.add(_driver(trial_ends_at=now_utc_naive() - timedelta(days=1)))
    await session.commit()
    now = now_utc_naive()
    old = now - timedelta(minutes=30)
    for pid in ("fake-paid", "fake-unpaid"):
        session.add(Payment(telegram_id=777000111, provider="fake", provider_payment_id=pid, kind="sbp",
                            amount_kop=100000, days=30, status="pending", created_at=old))
    await session.commit()
    provider.payments["fake-paid"] = ProviderPayment(
        id="fake-paid", status=STATUS_SUCCEEDED, amount_kop=100000, telegram_id=777000111, kind="sbp", days=30)
    provider.payments["fake-unpaid"] = ProviderPayment(
        id="fake-unpaid", status=STATUS_PENDING, amount_kop=100000, telegram_id=777000111, kind="sbp", days=30)

    credited = await billing_flow.reconcile_pending(provider=provider, now=now)

    assert credited == 1
    statuses = {p.provider_payment_id: p.status for p in (await session.execute(select(Payment))).scalars().all()}
    assert statuses == {"fake-paid": PAYMENT_SUCCEEDED, "fake-unpaid": PAYMENT_CANCELED}
    assert any("Подписка продлена" in text for _, text in sent_messages)  # водителю пришло сообщение


# --- Уведомления владельцу и журнал событий -------------------------------------------------------


@pytest.fixture
def owner_chat(monkeypatch):
    monkeypatch.setattr(settings, "alert_chat_id", "424242")


async def _events(session):
    from app.models import BillingEvent

    session.expire_all()
    return [e.kind for e in (await session.execute(select(BillingEvent).order_by(BillingEvent.id))).scalars().all()]


async def test_owner_gets_a_message_and_a_journal_entry_for_every_payment(
    client, session, subscription_on, telegram_login_on, provider, sent_messages, owner_chat
):
    await _login(client)
    await client.post("/subscription/pay", data={"kind": "sbp"})
    provider.succeed("fake-1")

    await client.post("/payments/yookassa", json={"event": "payment.succeeded", "object": {"id": "fake-1"}})
    await client.post("/payments/yookassa", json={"event": "payment.succeeded", "object": {"id": "fake-1"}})  # повтор

    to_owner = [text for chat, text in sent_messages if chat == 424242]
    assert len(to_owner) == 1  # повтор уведомления владельца не дёргает
    assert "💰 Оплата подписки" in to_owner[0] and "1000 ₽" in to_owner[0] and "СБП" in to_owner[0]
    assert USER in to_owner[0]
    kinds = await _events(session)
    assert kinds.count("payment_created") == 1 and kinds.count("payment_succeeded") == 1
    assert kinds.count("payment_duplicate") == 1 and kinds.count("webhook_received") == 2


async def test_owner_gets_a_message_about_a_refund(
    client, session, subscription_on, telegram_login_on, provider, sent_messages, owner_chat
):
    await _login(client)
    await client.post("/subscription/pay", data={"kind": "sbp"})
    provider.succeed("fake-1")
    await client.post("/payments/yookassa", json={"event": "payment.succeeded", "object": {"id": "fake-1"}})
    sent_messages.clear()
    provider.refund("ref-1", "fake-1", 100000)

    await client.post("/payments/yookassa", json={"event": "refund.succeeded", "object": {"id": "ref-1"}})

    to_owner = [text for chat, text in sent_messages if chat == 424242]
    assert len(to_owner) == 1 and "↩️ Возврат платежа" in to_owner[0] and "снято 30 дн." in to_owner[0]
    assert "refund_succeeded" in await _events(session)


async def test_suspicious_payments_alert_the_owner(session, subscription_on, sent_messages, owner_chat):
    session.add(_driver())
    await session.commit()
    cheap = ProviderPayment(id="p-cheap", status=STATUS_SUCCEEDED, amount_kop=100, telegram_id=777000111,
                            kind="sbp", days=30)
    nobody = ProviderPayment(id="p-nobody", status=STATUS_SUCCEEDED, amount_kop=100000, telegram_id=None,
                             kind="sbp", days=30)
    ghost = ProviderPayment(id="p-ghost", status=STATUS_SUCCEEDED, amount_kop=100000, telegram_id=1,
                            kind="sbp", days=30)

    for info in (cheap, nobody, ghost):
        await billing_flow.apply_payment(session, "fake", info)

    texts = [text for chat, text in sent_messages if chat == 424242]
    assert len(texts) == 3 and all(text.startswith("🚨") for text in texts)
    assert (await _events(session)).count("payment_anomaly") == 3


async def test_refund_of_an_unknown_payment_alerts_the_owner(session, subscription_on, sent_messages, owner_chat):
    from app.services.payments.base import ProviderRefund

    await billing_flow.apply_refund(
        session, ProviderRefund(id="r-x", status=STATUS_SUCCEEDED, payment_id="never-seen", amount_kop=100000)
    )

    texts = [text for chat, text in sent_messages if chat == 424242]
    assert len(texts) == 1 and "дни не списаны" in texts[0]


async def test_provider_failures_alert_the_owner_but_not_every_minute(
    client, session, subscription_on, telegram_login_on, provider, sent_messages, owner_chat, monkeypatch
):
    from app.services.payments import PaymentError

    async def down(**kwargs):
        raise PaymentError("нет связи")

    monkeypatch.setattr(provider, "create_payment", down)
    await _login(client)

    for _ in range(3):
        await client.post("/subscription/pay", data={"kind": "sbp"})

    texts = [text for chat, text in sent_messages if chat == 424242]
    assert len(texts) == 1 and "Не удалось создать платёж" in texts[0]  # throttle: лежащая ЮKassa не заваливает чат
    assert (await _events(session)).count("payment_create_failed") == 3  # а в журнале видно каждый сбой


async def test_without_owner_chat_nothing_is_sent_but_the_journal_still_works(
    client, session, subscription_on, telegram_login_on, provider, sent_messages, monkeypatch
):
    monkeypatch.setattr(settings, "alert_chat_id", "")
    await _login(client)
    await client.post("/subscription/pay", data={"kind": "sbp"})
    provider.succeed("fake-1")

    await client.post("/payments/yookassa", json={"event": "payment.succeeded", "object": {"id": "fake-1"}})

    assert [chat for chat, _ in sent_messages] == [int(USER)]  # только водителю
    assert "payment_succeeded" in await _events(session)


async def test_autorenew_failure_alerts_the_owner(session, subscription_on, provider, sent_messages, owner_chat):
    provider.charge_status = STATUS_CANCELED
    session.add(Driver(telegram_id=3030, trial_ends_at=NOW - timedelta(days=5), paid_until=NOW + timedelta(hours=5),
                       auto_renew=True, payment_method_id="pm-bad"))
    await session.commit()

    await billing_flow.autorenew_due(provider=provider, send=Messages(), now=NOW)

    texts = [text for chat, text in sent_messages if chat == 424242]
    assert len(texts) == 1 and "Автосписание не прошло" in texts[0]
    assert "autorenew_failed" in await _events(session)


# --- Админка: платежи и журнал ---------------------------------------------------------------------


async def test_admin_billing_page_shows_payments_events_and_refunds(
    client, session, subscription_on, telegram_login_on, provider, sent_messages
):
    session.add(Driver(telegram_id=8802, first_name="Платящий", username="payer", trial_ends_at=now_utc_naive()))
    await session.commit()
    info = ProviderPayment(id="adm-1", status=STATUS_SUCCEEDED, amount_kop=100000, telegram_id=8802, kind="sbp", days=30)
    await billing_flow.apply_payment(session, "fake", info)
    await client.get("/auth/telegram", params=_signed_params())  # тестовый админ

    page = await client.get("/admin/billing")

    assert page.status_code == 200
    assert "Журнал событий" in page.text and "оплата получена" in page.text and "adm-1" in page.text
    assert "@payer" in page.text and "1000 ₽" in page.text


async def test_admin_billing_page_is_hidden_from_non_admins(client, subscription_on, telegram_login_on):
    await _login(client)
    assert (await client.get("/admin/billing")).status_code == 404


# --- Подсказки: иконка в профиле, третий шаг про обратную связь --------------------------------------


async def test_tour_button_lives_in_the_profile_not_in_the_header(client, session, subscription_on, telegram_login_on):
    await _login(client)

    feed = (await client.get("/")).text
    profile = (await client.get("/profile")).text

    assert 'id="tourBtn"' not in feed  # из шапки на главном экране убрана
    assert 'href="/?tour=1"' in profile and "Показать подсказки" in profile


def test_tour_has_a_feedback_step_and_starts_from_the_profile_link():
    from pathlib import Path

    js = (Path(__file__).resolve().parent.parent / "app" / "web" / "static" / "tour.js").read_text(encoding="utf-8")

    assert ".collab-btn" in js and "Обратная связь" in js
    assert "ошибку" in js and "предложения" in js and "вопросы" in js
    assert "tour=1" in js  # запуск по ссылке из профиля


async def test_reconcile_closes_a_payment_the_provider_does_not_know_without_alarm(
    session, subscription_on, provider, sent_messages, owner_chat
):
    session.add(_driver())
    await session.commit()
    now = now_utc_naive()
    session.add(Payment(telegram_id=777000111, provider="old-shop", provider_payment_id="from-old-shop", kind="sbp",
                        amount_kop=100000, days=30, status="pending", created_at=now - timedelta(hours=2)))
    await session.commit()  # у нового магазина такого платежа нет: провайдер ответит 404

    await billing_flow.reconcile_pending(provider=provider, now=now)

    assert (await session.execute(select(Payment))).scalars().one().status == PAYMENT_CANCELED
    assert [text for chat, text in sent_messages if chat == 424242] == []  # ложной тревоги владельцу нет
