"""Метрики качества разбора заявок.

Без них невозможно понять, не «врёт» ли LLM: сколько сообщений вообще дошло до
разбора, какая доля заявок неполная, сколько тратим токенов и сколько это
длится. Пишется на каждое обработанное сообщение (одна строка), читается через
``scripts/parse_stats.py`` и админку ``/admin``.
"""

from typing import Optional

from sqlalchemy import BigInteger, Enum, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin
from app.models.enums import ParseOutcome


class ParseStat(Base, TimestampMixin):
    __tablename__ = "parse_stats"
    __table_args__ = (
        Index("ix_parse_stats_created", "created_at"),
        Index("ix_parse_stats_outcome", "outcome"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)

    chat_id: Mapped[Optional[int]] = mapped_column(BigInteger)
    message_id: Mapped[Optional[int]] = mapped_column(BigInteger)
    #: sha256 текста — по нему работает кэш разбора и поиск «того же сообщения».
    text_hash: Mapped[Optional[str]] = mapped_column(String(64))

    outcome: Mapped[ParseOutcome] = mapped_column(
        Enum(ParseOutcome, native_enum=False, length=24), nullable=False
    )
    orders_found: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    #: Суммарно неполных полей во всех найденных заявках.
    missing_fields: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    model: Mapped[Optional[str]] = mapped_column(String(64))
    prompt_tokens: Mapped[Optional[int]] = mapped_column(Integer)
    completion_tokens: Mapped[Optional[int]] = mapped_column(Integer)
    latency_ms: Mapped[Optional[int]] = mapped_column(Integer)
    attempts: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    error: Mapped[Optional[str]] = mapped_column(Text)

    def __repr__(self) -> str:
        return f"<ParseStat {self.outcome.value} orders={self.orders_found}>"
