"""orders.content_key: отпечаток содержимого заявки без подписи диспетчера

По нему агент отличает повтор заявки (та же заявка с другой подписью) от нового заказа до обращения к LLM.
Заполняется для новых заказов; у старых пусто. Переносима.

Revision ID: d3b7f9a1c5e8
Revises: c1a5e7d9b3f4
Create Date: 2026-10-12 21:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'd3b7f9a1c5e8'
down_revision: Union[str, None] = 'c1a5e7d9b3f4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('orders', sa.Column('content_key', sa.String(length=32), nullable=True))
    op.create_index('ix_orders_content_key', 'orders', ['content_key'])


def downgrade() -> None:
    op.drop_index('ix_orders_content_key', table_name='orders')
    op.drop_column('orders', 'content_key')
