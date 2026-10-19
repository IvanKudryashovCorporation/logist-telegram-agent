"""«Последний заход» в админке: обновляется при заходе вошедшего пользователя, а не только при входе."""

import asyncio
from datetime import timedelta

from sqlalchemy import select

from app.config import settings
from app.db.base import SessionLocal
from app.models import Driver
from app.timeutil import now_utc_naive
from app.web import last_seen
from app.web.deps import DRIVER_SESSION_COOKIE, driver_session_cookie_value

BOT_TOKEN = "123456:test-bot-token"


def test_touch_is_throttled():
    last_seen._last_touch.clear()

    assert last_seen.should_touch(1, now=1000.0) is True
    assert last_seen.should_touch(1, now=1000.0 + last_seen.TOUCH_INTERVAL_SECONDS - 1) is False
    assert last_seen.should_touch(2, now=1001.0) is True  # другой пользователь не мешает
    assert last_seen.should_touch(1, now=1000.0 + last_seen.TOUCH_INTERVAL_SECONDS + 1) is True


async def _last_login(telegram_id: int):
    async with SessionLocal() as session:
        return (await session.execute(select(Driver.last_login_at).where(Driver.telegram_id == telegram_id))).scalar_one()


async def test_visit_updates_last_login_of_signed_in_user(client, monkeypatch):
    monkeypatch.setattr(settings, "telegram_login_bot_token", BOT_TOKEN)
    monkeypatch.setattr(settings, "telegram_login_bot_username", "podacha_bot")
    last_seen._last_touch.clear()
    old = now_utc_naive() - timedelta(days=3)
    async with SessionLocal() as session:
        session.add(Driver(telegram_id=777001, first_name="Иван", last_login_at=old))
        await session.commit()

    await client.get("/about", cookies={DRIVER_SESSION_COOKIE: driver_session_cookie_value(777001)})
    await asyncio.sleep(0.3)

    assert await _last_login(777001) > old + timedelta(days=2)


async def test_anonymous_visit_changes_nothing(client, monkeypatch):
    monkeypatch.setattr(settings, "telegram_login_bot_token", BOT_TOKEN)
    monkeypatch.setattr(settings, "telegram_login_bot_username", "podacha_bot")
    last_seen._last_touch.clear()
    old = now_utc_naive() - timedelta(days=3)
    async with SessionLocal() as session:
        session.add(Driver(telegram_id=777002, first_name="Пётр", last_login_at=old))
        await session.commit()

    await client.get("/about")
    await asyncio.sleep(0.2)

    assert await _last_login(777002) == old
