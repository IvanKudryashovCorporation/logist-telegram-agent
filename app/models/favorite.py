"""Избранное водителя: заказы, которые он отметил сердечком в ленте.

Это закладка, а не «взятие»: заказ остаётся в общей ленте для всех. Владелец — тот же токен, что и у «взятых»
заказов (``tg:<id>`` после входа, иначе анонимная cookie).
"""

from sqlalchemy import ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin


class OrderFavorite(Base, TimestampMixin):
    __tablename__ = "order_favorites"
    __table_args__ = (UniqueConstraint("owner_token", "order_id", name="uq_order_favorites_owner_order"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    owner_token: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    order_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("orders.id", ondelete="CASCADE"), index=True, nullable=False
    )

    def __repr__(self) -> str:
        return f"<OrderFavorite {self.owner_token} order:{self.order_id}>"
