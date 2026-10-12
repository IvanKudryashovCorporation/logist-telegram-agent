"""последний применённый фильтр ленты у водителя

``drivers.saved_filter`` — JSON с параметрами ленты (города, радиус, дата, цена, сортировка). При следующем заходе без
параметров сайт открывает ленту с этим фильтром. Переносима.

Revision ID: c1a5e7d9b3f4
Revises: b7e3a9c1d5f2
Create Date: 2026-10-12 12:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'c1a5e7d9b3f4'
down_revision: Union[str, None] = 'b7e3a9c1d5f2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('drivers', sa.Column('saved_filter', sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column('drivers', 'saved_filter')
