"""История действий пользователя на сайте: заходы, фильтры, вход (оплаты и заказы лежат в своих журналах)."""

from sqlalchemy import BigInteger, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin


class UserEvent(Base, TimestampMixin):
    __tablename__ = "user_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    #: Короткий код события: login, visit, filter_applied, filter_saved, filter_deleted, ...
    kind: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    details: Mapped[str] = mapped_column(Text, nullable=False, default="")

    def __repr__(self) -> str:
        return f"<UserEvent {self.kind} tg:{self.telegram_id}>"
