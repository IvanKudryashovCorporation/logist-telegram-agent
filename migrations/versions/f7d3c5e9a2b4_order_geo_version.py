"""orders.geo_version: версия геокодера, которой посчитаны координаты

NULL у всех существующих заказов — фоновый воркер перегеокодирует их новой
логикой (регион из адреса и т.п.) и проставит текущую версию. Переносима:
SQLite и PostgreSQL.

Revision ID: f7d3c5e9a2b4
Revises: e6c2b4d8f3a1
Create Date: 2026-10-02 23:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'f7d3c5e9a2b4'
down_revision: Union[str, None] = 'e6c2b4d8f3a1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('orders') as batch:
        batch.add_column(sa.Column('geo_version', sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('orders') as batch:
        batch.drop_column('geo_version')
