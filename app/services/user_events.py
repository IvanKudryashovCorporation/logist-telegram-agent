"""Запись действий пользователя на сайте (см. app.models.user_event). Сбой записи страницу не ломает."""

import logging
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import SessionLocal
from app.models import UserEvent

log = logging.getLogger("user.events")

LOGIN = "login"
VISIT = "visit"
FILTER_APPLIED = "filter_applied"
FILTER_SAVED = "filter_saved"
FILTER_EXISTS = "filter_exists"
FILTER_LIMIT = "filter_limit"
FILTER_DELETED = "filter_deleted"
FILTER_ON = "filter_on"
FILTER_OFF = "filter_off"

#: Подписи для админки.
KIND_LABELS = {
    LOGIN: "Вход через Telegram",
    VISIT: "Зашёл на сайт",
    FILTER_APPLIED: "Применил фильтр",
    FILTER_SAVED: "Сохранил фильтр в «Мои фильтры»",
    FILTER_EXISTS: "Такой фильтр уже был сохранён",
    FILTER_LIMIT: "Не смог сохранить фильтр: достигнут лимит",
    FILTER_DELETED: "Удалил фильтр",
    FILTER_ON: "Включил уведомления по фильтру",
    FILTER_OFF: "Выключил уведомления по фильтру",
}

#: Один заход считаем новым, если прошло не меньше стольких минут с предыдущего.
VISIT_GAP_MINUTES = 30


async def record(
    kind: str, telegram_id: Optional[int], details: str = "", *, session: Optional[AsyncSession] = None
) -> None:
    """Пишет событие. ``session`` — если передана, строка добавляется в неё (коммитит вызывающий)."""
    if telegram_id is None:
        return
    event = UserEvent(telegram_id=telegram_id, kind=kind, details=details[:1000])
    try:
        if session is not None:
            session.add(event)
            return
        async with SessionLocal() as own:
            own.add(event)
            await own.commit()
    except Exception:
        log.warning("Не удалось записать событие %s пользователя %s", kind, telegram_id, exc_info=True)
