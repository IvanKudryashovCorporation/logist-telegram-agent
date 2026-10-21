"""аналитика: заходы на сайт и источники трафика

``site_visits`` — по строке на сеанс: откуда пришёл посетитель (Referer, utm, Telegram, ярлык приложения) и куда
попал. Читает страница ``/admin/traffic``. Переносима.

Revision ID: b2d6f8a1c4e7
Revises: a9c4e1b7d305
Create Date: 2026-10-21 12:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'b2d6f8a1c4e7'
down_revision: Union[str, None] = 'a9c4e1b7d305'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'site_visits',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('source', sa.String(length=48), nullable=False),
        sa.Column('referrer_host', sa.String(length=128), nullable=True),
        sa.Column('utm_source', sa.String(length=64), nullable=True),
        sa.Column('utm_medium', sa.String(length=64), nullable=True),
        sa.Column('utm_campaign', sa.String(length=64), nullable=True),
        sa.Column('landing', sa.String(length=96), nullable=False),
        sa.Column('device', sa.String(length=16), nullable=False),
        sa.Column('visitor', sa.String(length=16), nullable=False),
        sa.Column('ip_hash', sa.String(length=16), nullable=False),
        sa.Column('telegram_id', sa.BigInteger(), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )
    op.create_index('ix_site_visits_source', 'site_visits', ['source'])
    op.create_index('ix_site_visits_visitor', 'site_visits', ['visitor'])
    op.create_index('ix_site_visits_ip_hash', 'site_visits', ['ip_hash'])
    op.create_index('ix_site_visits_telegram_id', 'site_visits', ['telegram_id'])
    op.create_index('ix_site_visits_created_at', 'site_visits', ['created_at'])


def downgrade() -> None:
    op.drop_index('ix_site_visits_created_at', table_name='site_visits')
    op.drop_index('ix_site_visits_telegram_id', table_name='site_visits')
    op.drop_index('ix_site_visits_ip_hash', table_name='site_visits')
    op.drop_index('ix_site_visits_visitor', table_name='site_visits')
    op.drop_index('ix_site_visits_source', table_name='site_visits')
    op.drop_table('site_visits')
