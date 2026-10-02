"""orders.vehicle_type: легковой или минивэн

Колонка нужна фильтру «Тип авто». Заполняется при каждом сохранении заказа
(app.search.refresh_derived), а здесь — один раз для уже существующих строк.
Строки читаются и обновляются в Python: ``lower()`` в SQLite кириллицу не
понимает, а регулярное выражение одно на обе базы. Переносима: SQLite и
PostgreSQL.

Revision ID: a8e4d6f1b3c5
Revises: f7d3c5e9a2b4
Create Date: 2026-10-02 23:30:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'a8e4d6f1b3c5'
down_revision: Union[str, None] = 'f7d3c5e9a2b4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('orders') as batch:
        batch.add_column(sa.Column('vehicle_type', sa.String(length=8), nullable=True))

    from app.search import vehicle_type_of

    connection = op.get_bind()
    rows = connection.execute(sa.text('SELECT id, car_class, raw_text, passengers FROM orders')).fetchall()
    update = sa.text('UPDATE orders SET vehicle_type = :vehicle WHERE id = :id')
    for order_id, car_class, raw_text, passengers in rows:
        connection.execute(
            update, {'vehicle': vehicle_type_of(car_class, raw_text, passengers), 'id': order_id}
        )


def downgrade() -> None:
    with op.batch_alter_table('orders') as batch:
        batch.drop_column('vehicle_type')
