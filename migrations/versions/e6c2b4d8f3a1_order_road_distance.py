"""orders.distance_km и route_checked_at: расстояние по дорогам

Колонки заполняет фоновый воркер (OSRM), поэтому миграция только добавляет
пустые колонки — ничего не пересчитывает. Переносима: SQLite и PostgreSQL.

Revision ID: e6c2b4d8f3a1
Revises: d5b9a3c7e1f2
Create Date: 2026-10-02 22:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'e6c2b4d8f3a1'
down_revision: Union[str, None] = 'd5b9a3c7e1f2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('orders') as batch:
        batch.add_column(sa.Column('distance_km', sa.Float(), nullable=True))
        batch.add_column(sa.Column('route_checked_at', sa.DateTime(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('orders') as batch:
        batch.drop_column('route_checked_at')
        batch.drop_column('distance_km')
