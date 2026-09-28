"""add order contact_username (explicit "писать @..." override)

Revision ID: 7a3f323f2884
Revises: 7607935b76de
Create Date: 2026-09-28 18:55:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '7a3f323f2884'
down_revision: Union[str, None] = '7607935b76de'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('orders', schema=None) as batch_op:
        batch_op.add_column(sa.Column('contact_username', sa.String(length=64), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('orders', schema=None) as batch_op:
        batch_op.drop_column('contact_username')
