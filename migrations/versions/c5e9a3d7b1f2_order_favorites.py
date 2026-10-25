"""избранное: заказы, отмеченные сердечком

``order_favorites`` — закладки водителя (токен ``tg:<id>`` или анонимная cookie). Заказ при этом остаётся в общей
ленте. Переносима.

Revision ID: c5e9a3d7b1f2
Revises: b4d8f2a6c901
Create Date: 2026-10-25 14:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'c5e9a3d7b1f2'
down_revision: Union[str, None] = 'b4d8f2a6c901'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'order_favorites',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('owner_token', sa.String(length=64), nullable=False),
        sa.Column('order_id', sa.Integer(), sa.ForeignKey('orders.id', ondelete='CASCADE'), nullable=False),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint('owner_token', 'order_id', name='uq_order_favorites_owner_order'),
    )
    op.create_index('ix_order_favorites_owner_token', 'order_favorites', ['owner_token'])
    op.create_index('ix_order_favorites_order_id', 'order_favorites', ['order_id'])


def downgrade() -> None:
    op.drop_index('ix_order_favorites_order_id', table_name='order_favorites')
    op.drop_index('ix_order_favorites_owner_token', table_name='order_favorites')
    op.drop_table('order_favorites')
