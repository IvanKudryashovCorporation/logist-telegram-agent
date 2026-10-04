"""единые названия городов: «Мин воды» и «Минеральные Воды» — один город

Раньше справочник сокращений искал точную строку, и варианты с другим регистром,
пробелами или точками оставались «новыми» городами (три «Минеральных Воды» в подсказках
фильтра, а фильтр по одному вариантов не находил остальные). Теперь название
сводится к каноническому (``app.city_aliases.canonical_city_name``), а ключ поиска
строится по нему. Здесь — один раз для уже сохранённых заказов. Строки читаются и пишутся
из Python. Переносима: SQLite и PostgreSQL.

Revision ID: c0a6f8b3d5e7
Revises: b9f5e7a2c4d6
Create Date: 2026-10-04 18:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'c0a6f8b3d5e7'
down_revision: Union[str, None] = 'b9f5e7a2c4d6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    from app.city_aliases import canonical_city_name, city_key
    from app.search import SEARCH_FIELDS, build_search_text

    bind = op.get_bind()
    rows = bind.execute(
        sa.text("SELECT id, from_city, to_city, from_city_key, to_city_key FROM orders")
    ).fetchall()
    fields = ", ".join(SEARCH_FIELDS)
    for order_id, from_city, to_city, from_key, to_key in rows:
        new_from = canonical_city_name(from_city) or from_city
        new_to = canonical_city_name(to_city) or to_city
        new_from_key = city_key(new_from) or None
        new_to_key = city_key(new_to) or None
        if (new_from, new_to, new_from_key, new_to_key) == (from_city, to_city, from_key, to_key):
            continue
        bind.execute(
            sa.text(
                "UPDATE orders SET from_city = :from_city, to_city = :to_city, "
                "from_city_key = :from_key, to_city_key = :to_key WHERE id = :id"
            ),
            {"from_city": new_from, "to_city": new_to, "from_key": new_from_key,
             "to_key": new_to_key, "id": order_id},
        )
        values = bind.execute(sa.text(f"SELECT {fields} FROM orders WHERE id = :id"), {"id": order_id}).one()
        bind.execute(
            sa.text("UPDATE orders SET search_text = :text WHERE id = :id"),
            {"text": build_search_text(order_id, list(values)), "id": order_id},
        )


def downgrade() -> None:
    # Прежние написания восстановить нельзя (и незачем): они были ошибкой.
    pass
