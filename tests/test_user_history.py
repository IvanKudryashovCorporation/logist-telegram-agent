"""Подробная история пользователя в админке: заходы, фильтры, оплаты, действия с заказами."""

import asyncio
from datetime import timedelta

from sqlalchemy import select

from app.config import settings
from app.db.base import SessionLocal
from app.models import ActionLog, ActorType, BillingEvent, Driver, UserEvent
from app.services import reporting, user_events
from app.timeutil import now_utc_naive
from app.web import last_seen
from app.web.deps import DRIVER_SESSION_COOKIE, driver_session_cookie_value

BOT_TOKEN = "123456:test-bot-token"
TG_ID = 888001


async def _make_driver() -> Driver:
    async with SessionLocal() as session:
        driver = Driver(telegram_id=TG_ID, first_name="Иван", trial_ends_at=now_utc_naive() + timedelta(days=5))
        session.add(driver)
        await session.commit()
        await session.refresh(driver)
        return driver


async def test_history_merges_site_money_and_order_events():
    driver = await _make_driver()
    now = now_utc_naive()
    async with SessionLocal() as session:
        session.add_all([
            UserEvent(telegram_id=TG_ID, kind=user_events.FILTER_SAVED, details="Откуда: Пермь", created_at=now - timedelta(hours=3)),
            BillingEvent(kind="payment_succeeded", telegram_id=TG_ID, amount_kop=99000, details="30 дн.", created_at=now - timedelta(hours=2)),
            ActionLog(order_id=None, actor=ActorType.DRIVER, action="order_released",
                      details=f"token={driver.token[:8]}", created_at=now - timedelta(hours=1)),
            BillingEvent(kind="webhook_received", telegram_id=TG_ID, details="шум", created_at=now),  # служебное — не показываем
        ])
        await session.commit()

    detail = await reporting.user_detail(TG_ID)

    labels = [row["label"] for row in detail["timeline"]]
    assert labels == ["Отказался от заказа", "Оплата получена", "Сохранил фильтр в «Мои фильтры»"]
    assert detail["timeline"][1]["details"].startswith("990 ₽")
    assert detail["timeline"][2]["details"] == "Откуда: Пермь"


async def test_login_and_filter_actions_are_recorded(client, monkeypatch):
    monkeypatch.setattr(settings, "telegram_login_bot_token", BOT_TOKEN)
    monkeypatch.setattr(settings, "telegram_login_bot_username", "podacha_bot")
    await _make_driver()
    cookies = {DRIVER_SESSION_COOKIE: driver_session_cookie_value(TG_ID)}

    await client.post("/filter", data={"from_city": "Пермь", "to_city": "Сочи", "notify": "1"}, cookies=cookies)

    async with SessionLocal() as session:
        kinds = [k for (k,) in (await session.execute(
            select(UserEvent.kind).where(UserEvent.telegram_id == TG_ID).order_by(UserEvent.id))).all()]
    assert kinds[:2] == [user_events.FILTER_APPLIED, user_events.FILTER_SAVED]


async def test_new_visit_is_recorded_once_per_half_hour(client, monkeypatch):
    monkeypatch.setattr(settings, "telegram_login_bot_token", BOT_TOKEN)
    monkeypatch.setattr(settings, "telegram_login_bot_username", "podacha_bot")
    last_seen._last_touch.clear()
    await _make_driver()
    cookies = {DRIVER_SESSION_COOKIE: driver_session_cookie_value(TG_ID)}

    await client.get("/about", cookies=cookies)
    await asyncio.sleep(0.3)
    last_seen._last_touch.clear()  # прошло 5 минут: заход обновит время, но это тот же визит
    await client.get("/about", cookies=cookies)
    await asyncio.sleep(0.3)

    async with SessionLocal() as session:
        visits = (await session.execute(
            select(UserEvent).where(UserEvent.telegram_id == TG_ID, UserEvent.kind == user_events.VISIT))).scalars().all()
    assert len(visits) == 1


async def test_header_has_no_search_box_or_live_badge_and_shows_profile_tile(client, monkeypatch):
    monkeypatch.setattr(settings, "telegram_login_bot_token", BOT_TOKEN)
    monkeypatch.setattr(settings, "telegram_login_bot_username", "podacha_bot")
    async with SessionLocal() as session:
        session.add(Driver(telegram_id=TG_ID, first_name="Иван", username="ivan_driver",
                           photo_url="https://t.me/i/userpic/320/ivan.jpg", trial_ends_at=now_utc_naive() + timedelta(days=5)))
        await session.commit()
    cookies = {DRIVER_SESSION_COOKIE: driver_session_cookie_value(TG_ID)}

    guest = (await client.get("/")).text
    page = (await client.get("/", cookies=cookies)).text

    for html in (guest, page):
        assert "Город, телефон или цена" not in html  # поле поиска убрано
        assert "live-badge" not in html and "Live ·" not in html  # «Live» перенесён в админку
        assert 'id="sortSelect"' in html and 'id="filtersDetails"' in html and 'class="toolbar-row"' in html
    assert 'class="header-profile-tile' in page and "ivan_driver" in page and "userpic/320/ivan.jpg" in page
