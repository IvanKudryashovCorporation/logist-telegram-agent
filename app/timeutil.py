"""Единая точка правды про время.

В проекте живут ДВА разных вида datetime, и их важно не путать:

1. ``pickup_at`` — «гражданское» время подачи по местным часам (Крым/Кубань/
   Кавминводы — везде МСК). Диспетчер пишет «в 14:30», мы храним наивный
   datetime 14:30 БЕЗ конвертации в UTC. Сравнивать его нужно с
   :func:`now_msk_naive`.

2. ``created_at`` / ``updated_at`` / ``taken_at`` — служебные отметки в UTC.
   ``created_at`` проставляет ``server_default=func.now()`` (в SQLite и
   PostgreSQL ``CURRENT_TIMESTAMP`` — это UTC), ``taken_at`` пишем мы сами.
   Сравнивать их нужно с :func:`now_utc_naive`.

``datetime.utcnow()`` здесь намеренно не используется: она deprecated начиная
с Python 3.12 и возвращает наивный datetime, который легко перепутать с
локальным. Все функции ниже — обёртки над timezone-aware ``datetime.now()``.
"""

from datetime import datetime, timedelta, timezone

#: Часовой пояс, в котором диспетчеры пишут время подачи.
MSK = timezone(timedelta(hours=3), name="MSK")

UTC = timezone.utc


def now_utc_aware() -> datetime:
    """Текущий момент, timezone-aware, UTC."""
    return datetime.now(UTC)


def now_utc_naive() -> datetime:
    """Текущий момент в UTC как наивный datetime.

    Именно в таком виде в БД лежат ``created_at``/``updated_at``/``taken_at`` —
    сравнивать их надо с этим значением, а не с МСК.
    """
    return datetime.now(UTC).replace(tzinfo=None)


def now_msk_aware() -> datetime:
    """Текущий момент, timezone-aware, МСК."""
    return datetime.now(MSK)


def now_msk_naive() -> datetime:
    """Текущий момент по МСК как наивный datetime — для сравнения с ``pickup_at``."""
    return datetime.now(MSK).replace(tzinfo=None)


def to_utc_naive(value: datetime | None) -> datetime | None:
    """Приводит aware-datetime к наивному UTC; наивный возвращает как есть."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


def minutes_since(moment: datetime | None, *, now: datetime | None = None) -> int | None:
    """Сколько полных минут прошло с ``moment`` (не меньше нуля).

    ``now`` можно передать явно — так функцию удобно тестировать и получать
    согласованные значения в пределах одного запроса.
    """
    if moment is None:
        return None
    reference = now if now is not None else now_utc_naive()
    if moment.tzinfo is not None and reference.tzinfo is None:
        moment = to_utc_naive(moment)
    return max(0, int((reference - moment).total_seconds() // 60))


def minutes_until(moment: datetime | None, *, now: datetime | None = None) -> int | None:
    """Сколько полных минут осталось до ``moment`` (отрицательное — уже прошло).

    ``moment`` трактуется как время по МСК (см. ``pickup_at``).
    """
    if moment is None:
        return None
    reference = now if now is not None else now_msk_naive()
    return int((moment - reference).total_seconds() // 60)
