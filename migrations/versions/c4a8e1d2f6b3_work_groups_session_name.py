"""work_groups.session_name — группы второго Telegram-аккаунта

Агент может читать группы несколькими аккаунтами: у группы хранится имя файла
сессии, которой её слушать. NULL — основная сессия (поведение до миграции).

Revision ID: c4a8e1d2f6b3
Revises: b7e204d1c5a9
Create Date: 2026-10-02 14:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'c4a8e1d2f6b3'
down_revision: Union[str, None] = 'b7e204d1c5a9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('work_groups', sa.Column('session_name', sa.String(length=64), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('work_groups') as batch_op:
        batch_op.drop_column('session_name')
