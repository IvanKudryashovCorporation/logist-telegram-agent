"""orders: ключ текста сообщения для отсева точных копий без LLM

``text_key`` — короткий хэш нормализованного текста сообщения, из которого создан заказ (см.
``app.parsing.llm_parser.text_key``). По нему агент находит заказ, уже созданный из такой же копии
заявки, и не тратит на копию вызов LLM. У существующих заказов пусто (заполняется при пересохранении).
Переносима: SQLite и PostgreSQL.

Revision ID: c9e5a1b3d7f2
Revises: b8d4f0a2c6e1
Create Date: 2026-10-10 12:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'c9e5a1b3d7f2'
down_revision: Union[str, None] = 'b8d4f0a2c6e1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('orders') as batch:
        batch.add_column(sa.Column('text_key', sa.String(length=32), nullable=True))
        batch.create_index('ix_orders_text_key', ['text_key'])


def downgrade() -> None:
    with op.batch_alter_table('orders') as batch:
        batch.drop_index('ix_orders_text_key')
        batch.drop_column('text_key')
