"""every order without a pickup time is «в ближайшее время»

Раньше «в ближайшее время» ставилось только по тексту заявки («сейчас»,
«в течение часа»), а остальные заказы без времени подачи показывались как
«не указана». Теперь везде, где время определить не удалось (в т.ч. «3ч» без
времени суток), заказ «в ближайшее время» — флаг выставляем и старым заказам.

Revision ID: b7e204d1c5a9
Revises: a3f6d92c1e57
Create Date: 2026-10-02 12:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'b7e204d1c5a9'
down_revision: Union[str, None] = 'a3f6d92c1e57'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.get_bind().execute(
        sa.text("UPDATE orders SET pickup_asap = 1 WHERE pickup_at IS NULL AND pickup_asap = 0")
    )


def downgrade() -> None:
    # Данные не откатываем: прежнее различие «срочно / не указано» восстановить нечем.
    pass
