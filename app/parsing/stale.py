"""Протухшие сообщения: разбирать их через LLM бессмысленно.

Когда разбор отстаёт (сбой провайдера, лимиты), в очереди копятся сообщения, которые к моменту
разбора уже неактуальны: «сейчас», «в ближайшее время» или время без даты через полсуток — это
прошедший заказ, на сайте такой пропал бы сразу (``ASAP_EXPIRE_HOURS``). Заявки с названной датой
(«завтра», «12.10», «в пятницу») переживают сутки и больше, их отсеиваем только совсем старые.

Правило намеренно осторожное: сомнительное уходит в модель.
"""

import re
from datetime import datetime, timedelta
from typing import Final, Optional

from app.config import settings

#: Заявка с названной датой живёт дольше, но не бесконечно: через столько часов отсекаем любую.
HARD_MAX_AGE_HOURS: Final[int] = 72

_MONTHS = (
    "январ", "феврал", "март", "апрел", "ма[яй]", "июн", "июл", "август",
    "сентябр", "октябр", "ноябр", "декабр",
)
_WEEKDAYS = ("понедельник", "вторник", "сред[уы]", "четверг", "пятниц", "суббот", "воскресень")

#: Признаки названной даты: «12.10», «12/10», «12 октября», «завтра», «в пятницу».
_DATE_HINT_RE: Final[re.Pattern[str]] = re.compile(
    r"\b\d{1,2}[./]\d{1,2}\b"
    r"|\b\d{1,2}\s*(?:" + "|".join(_MONTHS) + r")"
    r"|\bзавтра\b|\bпослезавтра\b"
    r"|\b(?:" + "|".join(_WEEKDAYS) + r")",
    re.IGNORECASE,
)


def has_date_hint(text: str) -> bool:
    """Названа ли в тексте конкретная дата (а не «сейчас» и не время без даты)."""
    return bool(_DATE_HINT_RE.search(text or ""))


def is_stale(text: str, sent_at: Optional[datetime], now: datetime) -> bool:
    """Стоит ли пропустить сообщение, отправленное в ``sent_at`` (наивный UTC), без LLM."""
    if sent_at is None:
        return False
    age = now - sent_at
    if age >= timedelta(hours=HARD_MAX_AGE_HOURS):
        return True
    if age >= timedelta(hours=max(1, settings.asap_expire_hours)):
        return not has_date_hint(text)
    return False
