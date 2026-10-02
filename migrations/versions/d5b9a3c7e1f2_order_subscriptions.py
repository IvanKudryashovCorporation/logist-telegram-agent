"""order_subscriptions: подписки водителей на новые заказы по фильтру

Таблицы ``order_subscriptions`` (одна подписка на водителя) и
``subscription_notifications`` (что уже отправлено). Миграция написана
переносимо — работает и на SQLite, и на PostgreSQL.

Revision ID: d5b9a3c7e1f2
Revises: c4a8e1d2f6b3
Create Date: 2026-10-02 18:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'd5b9a3c7e1f2'
down_revision: Union[str, None] = 'c4a8e1d2f6b3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'order_subscriptions',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('telegram_id', sa.BigInteger(), nullable=False),
        sa.Column('params', sa.JSON(), nullable=False),
        sa.Column('is_active', sa.Boolean(), nullable=False),
        sa.Column('since', sa.DateTime(), nullable=False),
        sa.Column('last_notified_at', sa.DateTime(), nullable=True),
        sa.Column('error', sa.String(length=255), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        op.f('ix_order_subscriptions_telegram_id'), 'order_subscriptions', ['telegram_id'], unique=True
    )

    op.create_table(
        'subscription_notifications',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('subscription_id', sa.Integer(), nullable=False),
        sa.Column('order_id', sa.Integer(), nullable=False),
        sa.Column('sent_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['subscription_id'], ['order_subscriptions.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('subscription_id', 'order_id', name='uq_sub_notification'),
    )
    op.create_index(
        op.f('ix_subscription_notifications_subscription_id'),
        'subscription_notifications', ['subscription_id'], unique=False,
    )
    op.create_index(
        op.f('ix_subscription_notifications_order_id'),
        'subscription_notifications', ['order_id'], unique=False,
    )


def downgrade() -> None:
    op.drop_table('subscription_notifications')
    op.drop_index(op.f('ix_order_subscriptions_telegram_id'), table_name='order_subscriptions')
    op.drop_table('order_subscriptions')
