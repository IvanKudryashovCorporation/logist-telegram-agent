"""Дни, начисленные админом, не считаются оплатой подписки."""

from datetime import timedelta

from app.db.base import SessionLocal
from app.models import Driver
from app.models.payment import KIND_SBP, PAYMENT_SUCCEEDED, Payment
from app.services import billing, reporting
from app.timeutil import now_utc_naive


async def _driver(telegram_id: int) -> None:
    async with SessionLocal() as session:
        session.add(Driver(telegram_id=telegram_id, first_name=f"U{telegram_id}",
                           trial_ends_at=now_utc_naive() - timedelta(days=1)))
        await session.commit()


async def test_admin_grant_is_not_counted_as_a_paid_subscription(monkeypatch):
    monkeypatch.setattr(billing, "enabled", lambda: True)
    await _driver(901)  # дни начислил админ
    await _driver(902)  # заплатил сам
    async with SessionLocal() as session:
        await billing.grant_days(session, 901, 30)
        session.add(Payment(telegram_id=902, provider="yookassa", provider_payment_id="pay-902", kind=KIND_SBP,
                            amount_kop=99000, days=30, status=PAYMENT_SUCCEEDED, paid_at=now_utc_naive()))
        driver = await billing.get_driver(session, 902)
        driver.paid_until = now_utc_naive() + timedelta(days=30)
        await session.commit()

    overview = await reporting.users_overview()

    by_id = {u["driver"].telegram_id: u for u in overview["users"]}
    assert by_id[901]["paid_real"] is False and by_id[902]["paid_real"] is True
    assert overview["summary"]["paid"] == 1 and overview["summary"]["gifted"] == 1
    detail = await reporting.user_detail(901)
    assert detail["paid_real"] is False
