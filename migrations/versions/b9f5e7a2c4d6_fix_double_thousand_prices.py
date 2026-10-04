"""исправить цены, к которым тысячи применили дважды

Диспетчер пишет «4000 тыс» (то есть 4000 ₽), разбор умножал ещё раз: в базе
оказывались 4 000 000 ₽. Теперь это ловит ``ParsedOrder`` (app.parsing.schema),
а здесь — те, что уже сохранены: цена от 300 000 и кратная 1000 делится на 1000
(до двух раз), всё, что и после этого больше миллиона, обнуляется (цена
неизвестна лучше нереальной). Строки читаются и пишутся из Python, правило
скопировано сюда, чтобы миграция не зависела от кода приложения. Переносима:
SQLite и PostgreSQL.

Revision ID: b9f5e7a2c4d6
Revises: a8e4d6f1b3c5
Create Date: 2026-10-04 12:00:00.000000

"""
from decimal import Decimal
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'b9f5e7a2c4d6'
down_revision: Union[str, None] = 'a8e4d6f1b3c5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_IMPLAUSIBLE_FROM = Decimal(300_000)
_ABSURD_ABOVE = Decimal(1_000_000)


def _fixed(value: Decimal):
    for _ in range(2):
        if value >= _IMPLAUSIBLE_FROM and value % 1000 == 0:
            value = value / 1000
    return None if value > _ABSURD_ABOVE else value


def upgrade() -> None:
    bind = op.get_bind()
    rows = bind.execute(
        sa.text("SELECT id, client_price FROM orders WHERE client_price >= 300000")
    ).fetchall()
    update = sa.text("UPDATE orders SET client_price = :price WHERE id = :id")
    for order_id, price in rows:
        bind.execute(update, {"price": _fixed(Decimal(str(price))), "id": order_id})


def downgrade() -> None:
    # Исходные нереальные цены восстановить нельзя (и незачем).
    pass
