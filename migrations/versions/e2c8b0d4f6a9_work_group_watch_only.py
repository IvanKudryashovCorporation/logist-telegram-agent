"""work_groups: режим «наблюдение без вступления»

``watch_only`` — группа читается опросом истории (аккаунт в ней не состоит),
``username`` — по нему группа находится без вступления, ``last_message_id`` — до какого
сообщения уже прочитано. Колонки пустые/выключены у всех существующих групп: они читаются
событиями, как раньше. Переносима: SQLite и PostgreSQL.

Revision ID: e2c8b0d4f6a9
Revises: d1b7a9c4e6f8
Create Date: 2026-10-04 21:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'e2c8b0d4f6a9'
down_revision: Union[str, None] = 'd1b7a9c4e6f8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('work_groups') as batch:
        batch.add_column(sa.Column('watch_only', sa.Boolean(), nullable=False, server_default=sa.false()))
        batch.add_column(sa.Column('username', sa.String(length=64), nullable=True))
        batch.add_column(sa.Column('last_message_id', sa.BigInteger(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('work_groups') as batch:
        batch.drop_column('last_message_id')
        batch.drop_column('username')
        batch.drop_column('watch_only')
