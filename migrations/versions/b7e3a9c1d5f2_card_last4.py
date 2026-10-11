"""последние цифры и тип сохранённой карты

``drivers.card_last4`` и ``drivers.card_brand`` — чтобы показать водителю, какая карта привязана к автоплатежу.
Переносима.

Revision ID: b7e3a9c1d5f2
Revises: a6d2f8b4c0e3
Create Date: 2026-10-11 21:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'b7e3a9c1d5f2'
down_revision: Union[str, None] = 'a6d2f8b4c0e3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('drivers') as batch:
        batch.add_column(sa.Column('card_last4', sa.String(length=4), nullable=True))
        batch.add_column(sa.Column('card_brand', sa.String(length=24), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('drivers') as batch:
        batch.drop_column('card_brand')
        batch.drop_column('card_last4')
