"""add orders.pickup_asap, rescue «в течение часа» orders and prices written in thousands

* ``orders.pickup_asap`` — подача «сейчас / в ближайшее время / в течение часа»:
  конкретного времени нет, но на сайте это «в ближайшее время», а не «не указана».
* Бэкфилл без LLM: заказам без времени подачи, чей исходный текст говорит о
  срочности, ставим ``pickup_asap = 1``; ценам < 100 (диспетчер написал «25» или
  «14т» вместо 25000/14000) умножаем на 1000. Цен 100–999 на проде не было.

Строки читаются и пишутся из Python: ``lower()`` в SQLite кириллицу не понимает.

Revision ID: 8c4e1f7a2b90
Revises: 5b7d2e9a41c8
Create Date: 2026-10-01 18:00:00.000000

"""
import re
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '8c4e1f7a2b90'
down_revision: Union[str, None] = '5b7d2e9a41c8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Регэксп живёт прямо в миграции: она не должна зависеть от кода приложения,
# который потом изменится.
_ASAP_RE = re.compile(
    r"сейчас|ближайш|в течени[еи]\s+(?:часа|\d+\s*мин)|через\s+\d+\s*мин|"
    r"\+\s*\d+\s*мин|как можно (?:скорее|быстрее)",
    re.IGNORECASE,
)


def upgrade() -> None:
    op.add_column(
        'orders',
        sa.Column('pickup_asap', sa.Boolean(), nullable=False, server_default=sa.text('0')),
    )

    bind = op.get_bind()
    rows = bind.execute(
        sa.text("SELECT id, raw_text FROM orders WHERE pickup_at IS NULL")
    ).fetchall()
    asap_ids = [row.id for row in rows if row.raw_text and _ASAP_RE.search(row.raw_text)]
    for order_id in asap_ids:
        bind.execute(sa.text("UPDATE orders SET pickup_asap = 1 WHERE id = :id"), {"id": order_id})

    bind.execute(
        sa.text(
            "UPDATE orders SET client_price = client_price * 1000 "
            "WHERE client_price > 0 AND client_price < 100"
        )
    )


def downgrade() -> None:
    with op.batch_alter_table('orders') as batch_op:
        batch_op.drop_column('pickup_asap')
