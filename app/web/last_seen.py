"""Когда пользователь последний раз заходил на сайт.

Раньше ``drivers.last_login_at`` ставился только при входе через Telegram, а вход держится в cookie
месяцами: человек заходит каждый день, но в админке видна дата давнего входа. Теперь время обновляется
при каждом заходе вошедшего пользователя, но не чаще раза в :data:`TOUCH_INTERVAL_SECONDS`, чтобы не писать
в базу на каждый запрос.
"""

import asyncio
import logging
import time
from datetime import timedelta
from typing import Optional

from sqlalchemy import or_, select, update
from starlette.requests import Request

from app.db.base import SessionLocal
from app.models import Driver
from app.services import user_events
from app.timeutil import now_utc_naive
from app.web.deps import logged_in_telegram_id

log = logging.getLogger("web.last_seen")

#: Не чаще одной записи в базу на пользователя за этот срок.
TOUCH_INTERVAL_SECONDS = 300
#: Служебные адреса, обращения к которым заходом на сайт не считаются.
_IGNORED_PREFIXES = ("/static/", "/healthz", "/robots.txt", "/sitemap.xml", "/favicon.ico",
                     "/manifest.webmanifest", "/sw.js", "/payments/", "/admin")
_MAX_TRACKED = 20_000

_last_touch: dict[int, float] = {}


def should_touch(telegram_id: int, *, now: Optional[float] = None) -> bool:
    """True, если пора записать заход (и запоминает время записи)."""
    now = time.monotonic() if now is None else now
    previous = _last_touch.get(telegram_id)
    if previous is not None and now - previous < TOUCH_INTERVAL_SECONDS:
        return False
    if len(_last_touch) >= _MAX_TRACKED:
        _last_touch.clear()
    _last_touch[telegram_id] = now
    return True


async def touch(telegram_id: int) -> None:
    """Ставит время захода; сбой не должен мешать странице."""
    try:
        now = now_utc_naive()
        async with SessionLocal() as session:
            previous = (
                await session.execute(select(Driver.last_login_at).where(Driver.telegram_id == telegram_id))
            ).scalar_one_or_none()
            if previous is None or previous < now - timedelta(minutes=user_events.VISIT_GAP_MINUTES):
                await user_events.record(user_events.VISIT, telegram_id, session=session)
            await session.execute(
                update(Driver)
                .where(
                    Driver.telegram_id == telegram_id,
                    or_(Driver.last_login_at.is_(None),
                        Driver.last_login_at < now - timedelta(seconds=TOUCH_INTERVAL_SECONDS)),
                )
                .values(last_login_at=now)
            )
            await session.commit()
    except Exception:
        log.warning("Не удалось записать время захода пользователя %s", telegram_id, exc_info=True)


class LastSeenMiddleware:
    """Фиксирует заход вошедшего пользователя на страницу сайта."""

    def __init__(self, app) -> None:
        self.app = app
        self._tasks: set[asyncio.Task] = set()

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "http" and scope.get("method") == "GET" and not scope["path"].startswith(_IGNORED_PREFIXES):
            telegram_id = logged_in_telegram_id(Request(scope))
            if telegram_id is not None and should_touch(telegram_id):
                task = asyncio.create_task(touch(telegram_id))
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)
        await self.app(scope, receive, send)
