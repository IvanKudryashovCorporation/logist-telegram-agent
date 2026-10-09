"""orders: промежуточные пункты маршрута

``via_points`` — список остановок между «откуда» и «куда»: ``[{"name": "Соликамск", "lat": ..., "lon": ...}]``
(координаты проставляет геокодер). Пусто у всех существующих заказов. Переносима: SQLite и PostgreSQL.

Revision ID: b8d4f0a2c6e1
Revises: a7c3e9f1b5d4
Create Date: 2026-10-07 18:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'b8d4f0a2c6e1'
down_revision: Union[str, None] = 'a7c3e9f1b5d4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('orders') as batch:
        batch.add_column(sa.Column('via_points', sa.JSON(none_as_null=True), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('orders') as batch:
        batch.drop_column('via_points')
