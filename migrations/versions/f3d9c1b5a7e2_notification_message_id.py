"""subscription_notifications: id сообщения бота и отметка «убрано»

``message_id`` — номер сообщения, которое бот отправил водителю: по нему, если диспетчер
удалил заявку, сообщение удаляется или переписывается без неё. ``retracted_at`` — когда это
сделано (повторно не трогаем). У уже отправленных уведомлений обе колонки пустые: для них
ничего не удаляется. Переносима: SQLite и PostgreSQL.

Revision ID: f3d9c1b5a7e2
Revises: e2c8b0d4f6a9
Create Date: 2026-10-07 12:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'f3d9c1b5a7e2'
down_revision: Union[str, None] = 'e2c8b0d4f6a9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('subscription_notifications') as batch:
        batch.add_column(sa.Column('message_id', sa.BigInteger(), nullable=True))
        batch.add_column(sa.Column('retracted_at', sa.DateTime(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('subscription_notifications') as batch:
        batch.drop_column('retracted_at')
        batch.drop_column('message_id')
