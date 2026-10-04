"""пересчитать ключи городов и убрать накопившиеся дубли заявок

* ``from_city_key``/``to_city_key`` строятся новым ``city_key`` (ё = е, точки, тип
  населённого пункта не входит): «Поселок/Посёлок Грицовский» и «М.О./М. О.» — один ключ;
* дубли: заявки одного диспетчера на один маршрут и время (цена может быть разной —
  диспетчер её меняет), опубликованные повторно или в другую группу. Остаётся
  САМАЯ НОВАЯ (в ней актуальная цена), старые получают статус CANCELLED.

Строки читаются и пишутся из Python. Переносима: SQLite и PostgreSQL.

Revision ID: d1b7a9c4e6f8
Revises: c0a6f8b3d5e7
Create Date: 2026-10-04 20:00:00.000000

"""
import re
from datetime import datetime, timedelta
from typing import Optional, Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'd1b7a9c4e6f8'
down_revision: Union[str, None] = 'c0a6f8b3d5e7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TOLERANCE = timedelta(hours=3)
_ASAP_WINDOW = timedelta(hours=24)


def _as_datetime(value) -> Optional[datetime]:
    if value is None or isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value))


def _same_place(first, second) -> bool:
    from app.city_aliases import city_key, expand_city_term

    key_a, key_b = city_key(expand_city_term(first or "")), city_key(expand_city_term(second or ""))
    if not key_a or not key_b:
        return False
    if key_a == key_b:
        return True
    stem_a = re.sub(r"[^0-9a-zа-я]+", "", key_a)[:6]
    stem_b = re.sub(r"[^0-9a-zа-я]+", "", key_b)[:6]
    return len(stem_a) >= 5 and stem_a == stem_b


def duplicate_ids(rows) -> list[int]:
    """id заявок, которые надо отменить как дубли более новых (остаётся самая новая).

    ``rows``: ``(id, tg_id, username, from_city, to_city, pickup_at, pickup_asap, phone,
    passengers, created_at)`` живых заявок.
    """
    orders = sorted(rows, key=lambda row: row[0])
    cancelled: set[int] = set()
    for index, older in enumerate(orders):
        for newer in orders[index + 1:]:
            if newer[0] in cancelled and older[0] in cancelled:
                continue
            same_who = (older[1] and older[1] == newer[1]) or (
                older[2] and newer[2] and older[2].lower() == newer[2].lower()
            )
            if not same_who:
                continue
            at_old, at_new = _as_datetime(older[5]), _as_datetime(newer[5])
            if at_old is None and at_new is None:
                created_old, created_new = _as_datetime(older[9]), _as_datetime(newer[9])
                if not (created_old and created_new and created_new - created_old <= _ASAP_WINDOW):
                    continue
            elif at_old is None or at_new is None or abs(at_new - at_old) > _TOLERANCE:
                continue
            if not (_same_place(older[3], newer[3]) and _same_place(older[4], newer[4])):
                continue
            if (older[7] and newer[7] and older[7] != newer[7]) or (
                older[8] and newer[8] and older[8] != newer[8]
            ):
                continue
            cancelled.add(older[0])
            break
    return sorted(cancelled)


def upgrade() -> None:
    from app.city_aliases import canonical_city_name, city_key

    bind = op.get_bind()

    # 1. Ключи городов по новому city_key.
    rows = bind.execute(
        sa.text("SELECT id, from_city, to_city, from_city_key, to_city_key FROM orders")
    ).fetchall()
    update_keys = sa.text(
        "UPDATE orders SET from_city_key = :from_key, to_city_key = :to_key WHERE id = :id"
    )
    for order_id, from_city, to_city, from_key, to_key in rows:
        new_from = city_key(canonical_city_name(from_city) or from_city) or None
        new_to = city_key(canonical_city_name(to_city) or to_city) or None
        if (new_from, new_to) != (from_key, to_key):
            bind.execute(update_keys, {"from_key": new_from, "to_key": new_to, "id": order_id})

    # 2. Дубли среди живых заявок.
    live = bind.execute(
        sa.text(
            "SELECT id, dispatcher_tg_id, dispatcher_username, from_city, to_city, pickup_at, "
            "pickup_asap, client_phone, passengers, created_at FROM orders "
            "WHERE status IN ('NEW', 'NEEDS_CLARIFICATION') AND taken_by_token IS NULL"
        )
    ).fetchall()
    for order_id in duplicate_ids([tuple(row) for row in live]):
        bind.execute(
            sa.text("UPDATE orders SET status = 'CANCELLED' WHERE id = :id"), {"id": order_id}
        )


def downgrade() -> None:
    # Отменённые дубли и прежние ключи восстанавливать незачем.
    pass
