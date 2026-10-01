"""infer pickup date for old orders that name only a time

«00:30-1:00 Москва-Демянск», «15:40 Старый Крым-Симф», «22-23:00 Темрюк-…» —
время в заявке было, а даты нет, и сайт писал «подача не указана». Для таких
заказов без времени подачи берём время из начала исходного текста и дату
публикации (created_at, UTC -> МСК), с той же логикой «сегодня/завтра», что и в
app.parsing.order_builder.resolve_pickup_at. Заказ, который давно в прошлом,
фоновая очистка сама переведёт в EXPIRED.

Revision ID: a3f6d92c1e57
Revises: 8c4e1f7a2b90
Create Date: 2026-10-01 20:00:00.000000

"""
import re
from datetime import datetime, timedelta
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'a3f6d92c1e57'
down_revision: Union[str, None] = '8c4e1f7a2b90'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_PAST_TOLERANCE = timedelta(hours=2)
# «22-23:00» (начало без минут) или «00:30-1:00» / «15:40».
_RANGE_RE = re.compile(r"^\s*(\d{1,2})(?::(\d{2}))?\s*[-–]\s*\d{1,2}[:.]\d{2}")
_TIME_RE = re.compile(r"^\s*(\d{1,2}):(\d{2})")


def _start_time(text: str):
    match = _RANGE_RE.match(text) or _TIME_RE.match(text)
    if match is None:
        return None
    hour, minute = int(match.group(1)), int(match.group(2) or 0)
    return (hour, minute) if hour <= 23 and minute <= 59 else None


def upgrade() -> None:
    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            "SELECT id, raw_text, created_at FROM orders "
            "WHERE pickup_at IS NULL AND pickup_asap = 0"
        )
    ).fetchall()
    for row in rows:
        start = _start_time(row.raw_text or "")
        if start is None or not row.created_at:
            continue
        created = row.created_at
        if isinstance(created, str):
            created = datetime.fromisoformat(created)
        published = created + timedelta(hours=3)  # UTC -> МСК
        pickup = published.replace(hour=start[0], minute=start[1], second=0, microsecond=0)
        if pickup < published - _PAST_TOLERANCE:
            pickup += timedelta(days=1)
        bind.execute(
            sa.text("UPDATE orders SET pickup_at = :pickup, pickup_time_key = :key WHERE id = :id"),
            {"pickup": pickup.strftime("%Y-%m-%d %H:%M:%S.%f"), "key": f"{start[0]:02d}:{start[1]:02d}", "id": row.id},
        )


def downgrade() -> None:
    # Данные не откатываем: восстановить «не указана» нечем и незачем.
    pass
