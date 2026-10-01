"""add geo coordinates to orders and geo_places cache

Фильтр радиуса «откуда/куда»:

* ``orders.from_lat/from_lon/to_lat/to_lon`` — координаты концов маршрута;
* ``orders.geo_checked_at`` — NULL, пока геокодер заказом не занимался. У всех
  уже существующих заказов он NULL, поэтому фоновый воркер агента сам
  догеокодирует старые строки (бэкфилл не нужен и не блокирует миграцию);
* ``geo_places`` — кэш Nominatim: имя места -> кандидаты. Города из справочника
  ``CITY_COORDS`` в таблицу не копируются, код читает их напрямую.

Revision ID: 5b7d2e9a41c8
Revises: 11a0a82cebd4
Create Date: 2026-10-01 12:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '5b7d2e9a41c8'
down_revision: Union[str, None] = '11a0a82cebd4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('orders', sa.Column('from_lat', sa.Float(), nullable=True))
    op.add_column('orders', sa.Column('from_lon', sa.Float(), nullable=True))
    op.add_column('orders', sa.Column('to_lat', sa.Float(), nullable=True))
    op.add_column('orders', sa.Column('to_lon', sa.Float(), nullable=True))
    op.add_column('orders', sa.Column('geo_checked_at', sa.DateTime(), nullable=True))

    op.create_table(
        'geo_places',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('key', sa.String(length=200), nullable=False),
        sa.Column('candidates', sa.JSON(), nullable=False),
        sa.Column('status', sa.String(length=16), nullable=False),
        sa.Column('source', sa.String(length=16), nullable=False),
        sa.Column('checked_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_geo_places_key'), 'geo_places', ['key'], unique=True)


def downgrade() -> None:
    op.drop_index(op.f('ix_geo_places_key'), table_name='geo_places')
    op.drop_table('geo_places')
    with op.batch_alter_table('orders') as batch_op:
        batch_op.drop_column('geo_checked_at')
        batch_op.drop_column('to_lon')
        batch_op.drop_column('to_lat')
        batch_op.drop_column('from_lon')
        batch_op.drop_column('from_lat')
