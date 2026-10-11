"""подписка: журнал событий

``billing_events`` — журнал платежей, уведомлений ЮKassa, возвратов, автосписаний и сбоев; его читает страница
``/admin/billing``. Переносима.

Revision ID: f4b8d0c2e6a1
Revises: e3a7c9b5d1f8
Create Date: 2026-10-11 18:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'f4b8d0c2e6a1'
down_revision: Union[str, None] = 'e3a7c9b5d1f8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'billing_events',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('kind', sa.String(length=32), nullable=False),
        sa.Column('telegram_id', sa.BigInteger(), nullable=True),
        sa.Column('provider_id', sa.String(length=64), nullable=True),
        sa.Column('amount_kop', sa.Integer(), nullable=True),
        sa.Column('details', sa.Text(), nullable=False),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )
    op.create_index('ix_billing_events_kind', 'billing_events', ['kind'])
    op.create_index('ix_billing_events_telegram_id', 'billing_events', ['telegram_id'])
    op.create_index('ix_billing_events_provider_id', 'billing_events', ['provider_id'])
    op.create_index('ix_billing_events_created_at', 'billing_events', ['created_at'])


def downgrade() -> None:
    op.drop_index('ix_billing_events_created_at', table_name='billing_events')
    op.drop_index('ix_billing_events_provider_id', table_name='billing_events')
    op.drop_index('ix_billing_events_telegram_id', table_name='billing_events')
    op.drop_index('ix_billing_events_kind', table_name='billing_events')
    op.drop_table('billing_events')
