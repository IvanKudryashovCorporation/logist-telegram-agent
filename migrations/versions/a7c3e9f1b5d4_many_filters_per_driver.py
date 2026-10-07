"""order_subscriptions: несколько фильтров у одного водителя

Раньше на водителя была одна подписка (уникальный индекс по ``telegram_id``). Теперь у него
несколько сохранённых фильтров («Мои фильтры»), у каждого своё включение уведомлений. Данные не
меняются: у каждого водителя по-прежнему та подписка, что была. Переносима: SQLite и PostgreSQL.

Revision ID: a7c3e9f1b5d4
Revises: f3d9c1b5a7e2
Create Date: 2026-10-07 15:00:00.000000

"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'a7c3e9f1b5d4'
down_revision: Union[str, None] = 'f3d9c1b5a7e2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_index(op.f('ix_order_subscriptions_telegram_id'), table_name='order_subscriptions')
    op.create_index(
        op.f('ix_order_subscriptions_telegram_id'), 'order_subscriptions', ['telegram_id'], unique=False
    )


def downgrade() -> None:
    # Вернуть уникальность можно, только если у каждого водителя не больше одного фильтра.
    op.drop_index(op.f('ix_order_subscriptions_telegram_id'), table_name='order_subscriptions')
    op.create_index(
        op.f('ix_order_subscriptions_telegram_id'), 'order_subscriptions', ['telegram_id'], unique=True
    )
