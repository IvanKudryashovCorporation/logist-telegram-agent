"""Уведомления на сайте: добавить, показать водителю, отметить прочитанными.

Водитель видит свои уведомления и общие рассылки, отправленные после его регистрации. «Непрочитанные» — те, что
новее ``Driver.notifications_seen_at`` (открытие колокольчика сдвигает отметку на «сейчас»).
Запись уведомления не должна ломать основное действие (платёж, напоминание), поэтому ``add`` не бросает исключений.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import SessionLocal
from app.models import Driver
from app.models.notification import KIND_INFO, Notification
from app.timeutil import now_utc_naive

log = logging.getLogger("notifications")

#: Допуск при сравнении меток времени: приветствие создаётся в момент регистрации, а метки из БД и из кода
#: (БД и Python хранят время с разной точностью) могут разойтись на долю секунды.
_TOLERANCE = timedelta(seconds=2)

#: Сколько уведомлений показываем в колокольчике.
PANEL_LIMIT = 15


@dataclass(frozen=True)
class Bell:
    """Содержимое колокольчика: последние уведомления и число непрочитанных."""

    items: list
    unread: int
    seen: Optional[datetime] = None


async def add(
    telegram_id: Optional[int], title: str, text: str = "", *, kind: str = KIND_INFO, url: Optional[str] = None,
    session: Optional[AsyncSession] = None,
) -> None:
    """Добавляет уведомление (``telegram_id=None`` — всем). Не бросает исключение."""
    item = Notification(telegram_id=telegram_id, kind=kind, title=title[:120], text=text, url=url)
    try:
        if session is not None:
            session.add(item)
            return
        async with SessionLocal() as own:
            own.add(item)
            await own.commit()
    except Exception:
        log.exception("Не удалось записать уведомление «%s»", title)


async def broadcast(title: str, text: str = "", *, url: Optional[str] = None) -> None:
    """Объявление для всех водителей (видят зарегистрированные до и после отправки, пока оно в ленте)."""
    await add(None, title, text, url=url)


def _visible(driver: Driver):
    """Условие «это уведомление для этого водителя»: личные и общие, появившиеся после его регистрации."""
    return (
        or_(Notification.telegram_id == driver.telegram_id, Notification.telegram_id.is_(None)),
        Notification.created_at >= driver.created_at - _TOLERANCE,
    )


async def bell_for(session: AsyncSession, telegram_id: Optional[int]) -> Bell:
    """Колокольчик водителя. Гостю и неизвестному id — пусто."""
    if telegram_id is None:
        return Bell([], 0)
    driver = (
        await session.execute(select(Driver).where(Driver.telegram_id == telegram_id))
    ).scalar_one_or_none()
    if driver is None:
        return Bell([], 0)
    seen: datetime = driver.notifications_seen_at or (driver.created_at - _TOLERANCE)
    items = list(
        (
            await session.execute(
                select(Notification).where(*_visible(driver)).order_by(Notification.id.desc()).limit(PANEL_LIMIT)
            )
        ).scalars().all()
    )
    unread = (
        await session.execute(
            select(func.count(Notification.id)).where(*_visible(driver), Notification.created_at > seen)
        )
    ).scalar_one()
    return Bell(items, int(unread), seen)


async def mark_seen(session: AsyncSession, telegram_id: int, *, now: Optional[datetime] = None) -> bool:
    """Отмечает всё прочитанным (``notifications_seen_at`` = сейчас). ``False`` — водителя нет."""
    driver = (
        await session.execute(select(Driver).where(Driver.telegram_id == telegram_id))
    ).scalar_one_or_none()
    if driver is None:
        return False
    driver.notifications_seen_at = now or now_utc_naive()
    await session.commit()
    return True
