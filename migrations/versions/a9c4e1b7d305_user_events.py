"""user_events: история действий пользователя на сайте

Заходы, вход через Telegram, применённые и сохранённые фильтры. Показывается в карточке пользователя в админке.
Переносима.

Revision ID: a9c4e1b7d305
Revises: d3b7f9a1c5e8
Create Date: 2026-10-19 18:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'a9c4e1b7d305'
down_revision: Union[str, None] = 'd3b7f9a1c5e8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'user_events',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('telegram_id', sa.BigInteger(), nullable=False),
        sa.Column('kind', sa.String(length=32), nullable=False),
        sa.Column('details', sa.Text(), nullable=False),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )
    op.create_index('ix_user_events_telegram_id', 'user_events', ['telegram_id'])
    op.create_index('ix_user_events_kind', 'user_events', ['kind'])


def downgrade() -> None:
    op.drop_index('ix_user_events_kind', table_name='user_events')
    op.drop_index('ix_user_events_telegram_id', table_name='user_events')
    op.drop_table('user_events')
