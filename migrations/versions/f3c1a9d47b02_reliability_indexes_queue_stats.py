"""reliability: unique source, feed indexes, search_text, queue + stats

Добавляет то, без чего проект ломается под нагрузкой и в проде:

* ``uq_orders_source`` — уникальность (чат, сообщение, индекс заявки внутри
  сообщения). In-memory дедупликация Telethon-событий не переживает каждый
  рестарт агента, это единственный надёжный барьер против дублей.
* ``ix_orders_feed`` / ``ix_orders_dedup`` — индексы под запрос ленты, счётчики
  в шапке и поиск дублей.
* ``orders.search_text`` — денормализованная строка для поиска (см. app/search.py).
* ``pending_messages`` — очередь сообщений, которые не удалось разобрать.
* ``parse_stats`` — метрики качества разбора.
* удаляет ``orders.driver_payment`` — дубль ``client_price``, который нигде не читался.

Новое значение ``OrderStatus.EXPIRED`` миграции НЕ требует: колонка статуса —
это ``Enum(native_enum=False)`` = VARCHAR(32) без CHECK-констрейнта.

Revision ID: f3c1a9d47b02
Revises: c37314caa784
Create Date: 2026-09-29 23:40:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'f3c1a9d47b02'
down_revision: Union[str, None] = 'c37314caa784'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_SEARCH_FIELDS = (
    "client_phone",
    "client_name",
    "from_city",
    "to_city",
    "from_address",
    "to_address",
    "dispatcher_username",
    "contact_username",
)


def _dedupe_orders(bind) -> int:
    """Удаляет дубли источника, оставляя заказ с минимальным id.

    Без этого ``create_unique_constraint`` на непустой базе упадёт. Дубли
    реально появляются, если агент перезапустился и Telethon переиграл события.
    """
    rows = bind.execute(
        sa.text(
            "SELECT source_chat_id, source_message_id, source_sub_index, MIN(id) AS keep_id, "
            "COUNT(*) AS n FROM orders GROUP BY source_chat_id, source_message_id, "
            "source_sub_index HAVING COUNT(*) > 1"
        )
    ).fetchall()
    removed = 0
    for chat_id, message_id, sub_index, keep_id, count in rows:
        result = bind.execute(
            sa.text(
                "DELETE FROM orders WHERE source_chat_id = :c AND source_message_id = :m "
                "AND source_sub_index = :s AND id != :keep"
            ),
            {"c": chat_id, "m": message_id, "s": sub_index, "keep": keep_id},
        )
        removed += result.rowcount or (count - 1)
    return removed


def _backfill_derived(bind) -> int:
    """Заполняет search_text / from_city_key / to_city_key для существующих заказов.

    Логика нормализации берётся из самого приложения (app.search, app.city_aliases),
    чтобы бэкфилл не мог разойтись с тем, что пишет рантайм.
    """
    from app.city_aliases import city_key
    from app.search import SEARCH_FIELDS, build_search_text, pickup_time_key

    columns = ", ".join(_SEARCH_FIELDS)
    rows = bind.execute(
        sa.text(f"SELECT id, {columns}, from_city, to_city, pickup_at FROM orders")
    ).fetchall()
    offset = 1 + len(SEARCH_FIELDS)
    for row in rows:
        bind.execute(
            sa.text(
                "UPDATE orders SET search_text = :search_text, "
                "from_city_key = :from_key, to_city_key = :to_key, "
                "pickup_time_key = :time_key WHERE id = :order_id"
            ),
            {
                "search_text": build_search_text(row[0], row[1:offset]),
                "from_key": city_key(row[offset]) or None,
                "to_key": city_key(row[offset + 1]) or None,
                "time_key": pickup_time_key(row[offset + 2]),
                "order_id": row[0],
            },
        )
    return len(rows)

def upgrade() -> None:
    bind = op.get_bind()
    removed = _dedupe_orders(bind)
    if removed:
        print(f"[migration] удалено дублей заказов по источнику: {removed}")

    with op.batch_alter_table('orders', schema=None) as batch_op:
        batch_op.add_column(sa.Column('search_text', sa.Text(), nullable=True))
        batch_op.add_column(sa.Column('from_city_key', sa.String(length=128), nullable=True))
        batch_op.add_column(sa.Column('to_city_key', sa.String(length=128), nullable=True))
        batch_op.add_column(sa.Column('pickup_time_key', sa.String(length=5), nullable=True))
        batch_op.create_unique_constraint(
            'uq_orders_source',
            ['source_chat_id', 'source_message_id', 'source_sub_index'],
        )
        batch_op.create_index('ix_orders_feed', ['status', 'taken_by_token', 'pickup_at'])
        batch_op.create_index('ix_orders_dedup', ['client_price', 'pickup_at'])
        batch_op.create_index('ix_orders_from_city_key', ['from_city_key'])
        batch_op.create_index('ix_orders_to_city_key', ['to_city_key'])
        batch_op.create_index('ix_orders_pickup_time_key', ['pickup_time_key'])
        batch_op.drop_column('driver_payment')

    filled = _backfill_derived(bind)
    if filled:
        print(f"[migration] поисковые поля заполнены для заказов: {filled}")

    op.create_table(
        'pending_messages',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('chat_id', sa.BigInteger(), nullable=False),
        sa.Column('message_id', sa.BigInteger(), nullable=False),
        sa.Column('is_edit', sa.Boolean(), nullable=False),
        sa.Column('text', sa.Text(), nullable=False),
        sa.Column(
            'status',
            sa.Enum(
                'PENDING', 'DONE', 'FAILED',
                name='pendingstatus', native_enum=False, length=16,
            ),
            nullable=False,
        ),
        sa.Column('attempts', sa.Integer(), nullable=False),
        sa.Column('next_attempt_at', sa.DateTime(), nullable=True),
        sa.Column('last_error', sa.Text(), nullable=True),
        sa.Column(
            'created_at', sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.Column(
            'updated_at', sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('chat_id', 'message_id', name='uq_pending_messages_source'),
    )
    op.create_index(
        'ix_pending_messages_due', 'pending_messages', ['status', 'next_attempt_at']
    )

    op.create_table(
        'parse_stats',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('chat_id', sa.BigInteger(), nullable=True),
        sa.Column('message_id', sa.BigInteger(), nullable=True),
        sa.Column('text_hash', sa.String(length=64), nullable=True),
        sa.Column(
            'outcome',
            sa.Enum(
                'ORDER', 'NOT_ORDER', 'PREFILTERED', 'DUPLICATE', 'ERROR', 'QUEUED',
                name='parseoutcome', native_enum=False, length=24,
            ),
            nullable=False,
        ),
        sa.Column('orders_found', sa.Integer(), nullable=False),
        sa.Column('missing_fields', sa.Integer(), nullable=False),
        sa.Column('model', sa.String(length=64), nullable=True),
        sa.Column('prompt_tokens', sa.Integer(), nullable=True),
        sa.Column('completion_tokens', sa.Integer(), nullable=True),
        sa.Column('latency_ms', sa.Integer(), nullable=True),
        sa.Column('attempts', sa.Integer(), nullable=False),
        sa.Column('error', sa.Text(), nullable=True),
        sa.Column(
            'created_at', sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.Column(
            'updated_at', sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_parse_stats_created', 'parse_stats', ['created_at'])
    op.create_index('ix_parse_stats_outcome', 'parse_stats', ['outcome'])


def downgrade() -> None:
    op.drop_index('ix_parse_stats_outcome', table_name='parse_stats')
    op.drop_index('ix_parse_stats_created', table_name='parse_stats')
    op.drop_table('parse_stats')

    op.drop_index('ix_pending_messages_due', table_name='pending_messages')
    op.drop_table('pending_messages')

    with op.batch_alter_table('orders', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column('driver_payment', sa.Numeric(precision=10, scale=2), nullable=True)
        )
        batch_op.drop_index('ix_orders_pickup_time_key')
        batch_op.drop_index('ix_orders_to_city_key')
        batch_op.drop_index('ix_orders_from_city_key')
        batch_op.drop_index('ix_orders_dedup')
        batch_op.drop_index('ix_orders_feed')
        batch_op.drop_constraint('uq_orders_source', type_='unique')
        batch_op.drop_column('pickup_time_key')
        batch_op.drop_column('to_city_key')
        batch_op.drop_column('from_city_key')
        batch_op.drop_column('search_text')

    # driver_payment исторически был синонимом client_price — восстанавливаем как было.
    op.get_bind().execute(sa.text("UPDATE orders SET driver_payment = client_price"))

