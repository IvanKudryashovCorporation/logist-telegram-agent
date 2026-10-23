"""parse_stats.delay_ms: сколько прошло от публикации сообщения до заказа в ленте

Для контроля цели «не больше 5 секунд»: медиана и p95 видны в админке. Переносима.

Revision ID: b4d8f2a6c901
Revises: b2d6f8a1c4e7
Create Date: 2026-10-22 18:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'b4d8f2a6c901'
down_revision: Union[str, None] = 'b2d6f8a1c4e7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('parse_stats') as batch:
        batch.add_column(sa.Column('delay_ms', sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('parse_stats') as batch:
        batch.drop_column('delay_ms')
