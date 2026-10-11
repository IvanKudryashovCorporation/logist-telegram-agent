"""подписка: поля водителя и таблица платежей

``drivers``: ``trial_ends_at``, ``paid_until``, ``auto_renew``, ``payment_method_id``, ``renew_failures``,
``reminded_for``. ``payments`` — платежи за подписку (``provider_payment_id`` уникален: начисление идемпотентно).
У существующих водителей новые поля пустые: подписка выключена, пока не задан ``SUBSCRIPTION_LAUNCH_DATE``.
Переносима.

Revision ID: d2f6b8a4c1e9
Revises: c9e5a1b3d7f2
Create Date: 2026-10-11 12:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'd2f6b8a4c1e9'
down_revision: Union[str, None] = 'c9e5a1b3d7f2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('drivers') as batch:
        batch.add_column(sa.Column('trial_ends_at', sa.DateTime(), nullable=True))
        batch.add_column(sa.Column('paid_until', sa.DateTime(), nullable=True))
        batch.add_column(sa.Column('auto_renew', sa.Boolean(), server_default=sa.false(), nullable=False))
        batch.add_column(sa.Column('payment_method_id', sa.String(length=64), nullable=True))
        batch.add_column(sa.Column('renew_failures', sa.Integer(), server_default='0', nullable=False))
        batch.add_column(sa.Column('reminded_for', sa.Date(), nullable=True))

    op.create_table(
        'payments',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('telegram_id', sa.BigInteger(), nullable=False),
        sa.Column('provider', sa.String(length=32), nullable=False),
        sa.Column('provider_payment_id', sa.String(length=64), nullable=False),
        sa.Column('kind', sa.String(length=16), nullable=False),
        sa.Column('amount_kop', sa.Integer(), nullable=False),
        sa.Column('days', sa.Integer(), nullable=False),
        sa.Column('status', sa.String(length=16), nullable=False),
        sa.Column('paid_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint('provider_payment_id', name='uq_payments_provider_payment_id'),
    )
    op.create_index('ix_payments_telegram_id', 'payments', ['telegram_id'])


def downgrade() -> None:
    op.drop_index('ix_payments_telegram_id', table_name='payments')
    op.drop_table('payments')
    with op.batch_alter_table('drivers') as batch:
        batch.drop_column('reminded_for')
        batch.drop_column('renew_failures')
        batch.drop_column('payment_method_id')
        batch.drop_column('auto_renew')
        batch.drop_column('paid_until')
        batch.drop_column('trial_ends_at')
