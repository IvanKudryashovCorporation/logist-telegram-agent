"""подписка: возвраты платежей

``refunds`` — возвраты по платежам за подписку (``provider_refund_id`` уникален: повторное уведомление ЮKassa
не снимает дни второй раз). Переносима: SQLite и PostgreSQL.

Revision ID: e3a7c9b5d1f8
Revises: d2f6b8a4c1e9
Create Date: 2026-10-11 16:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'e3a7c9b5d1f8'
down_revision: Union[str, None] = 'd2f6b8a4c1e9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'refunds',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('provider_refund_id', sa.String(length=64), nullable=False),
        sa.Column('provider_payment_id', sa.String(length=64), nullable=False),
        sa.Column('telegram_id', sa.BigInteger(), nullable=False),
        sa.Column('amount_kop', sa.Integer(), nullable=False),
        sa.Column('days', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint('provider_refund_id', name='uq_refunds_provider_refund_id'),
    )
    op.create_index('ix_refunds_provider_payment_id', 'refunds', ['provider_payment_id'])
    op.create_index('ix_refunds_telegram_id', 'refunds', ['telegram_id'])


def downgrade() -> None:
    op.drop_index('ix_refunds_telegram_id', table_name='refunds')
    op.drop_index('ix_refunds_provider_payment_id', table_name='refunds')
    op.drop_table('refunds')
