"""уведомления на сайте (колокольчик)

``notifications`` — уведомления водителю (оплата, подписка, объявления владельца), ``drivers.notifications_seen_at`` —
когда водитель последний раз открывал колокольчик. Переносима.

Revision ID: a6d2f8b4c0e3
Revises: f4b8d0c2e6a1
Create Date: 2026-10-11 20:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'a6d2f8b4c0e3'
down_revision: Union[str, None] = 'f4b8d0c2e6a1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('drivers') as batch:
        batch.add_column(sa.Column('notifications_seen_at', sa.DateTime(), nullable=True))
    op.create_table(
        'notifications',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('telegram_id', sa.BigInteger(), nullable=True),
        sa.Column('kind', sa.String(length=16), nullable=False),
        sa.Column('title', sa.String(length=120), nullable=False),
        sa.Column('text', sa.Text(), nullable=False),
        sa.Column('url', sa.String(length=300), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )
    op.create_index('ix_notifications_telegram_id', 'notifications', ['telegram_id'])


def downgrade() -> None:
    op.drop_index('ix_notifications_telegram_id', table_name='notifications')
    op.drop_table('notifications')
    with op.batch_alter_table('drivers') as batch:
        batch.drop_column('notifications_seen_at')
