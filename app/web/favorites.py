"""Избранное: сердечко в строке заказа. Закладка не прячет заказ из ленты и ничего не «берёт»."""

from typing import Iterable

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Order, OrderFavorite


async def favorite_ids(session: AsyncSession, token: str, order_ids: Iterable[int]) -> set[int]:
    """Какие из этих заказов у водителя в избранном."""
    ids = list(order_ids)
    if not token or not ids:
        return set()
    rows = await session.execute(
        select(OrderFavorite.order_id).where(OrderFavorite.owner_token == token, OrderFavorite.order_id.in_(ids))
    )
    return set(rows.scalars().all())


async def toggle_favorite(session: AsyncSession, token: str, order_id: int) -> bool | None:
    """Ставит или снимает сердечко. Возвращает новое состояние, ``None`` — такого заказа нет."""
    if await session.get(Order, order_id) is None:
        return None
    existing = (
        await session.execute(
            select(OrderFavorite).where(OrderFavorite.owner_token == token, OrderFavorite.order_id == order_id)
        )
    ).scalar_one_or_none()
    if existing is not None:
        await session.execute(delete(OrderFavorite).where(OrderFavorite.id == existing.id))
        await session.commit()
        return False
    session.add(OrderFavorite(owner_token=token, order_id=order_id))
    try:
        await session.commit()
    except IntegrityError:  # двойной клик: вторая вставка упёрлась в уникальный ключ — уже в избранном
        await session.rollback()
    return True


async def favorite_orders(session: AsyncSession, token: str) -> list[Order]:
    """Избранные заказы водителя: недавно добавленные сверху."""
    rows = await session.execute(
        select(Order)
        .join(OrderFavorite, OrderFavorite.order_id == Order.id)
        .where(OrderFavorite.owner_token == token)
        .order_by(OrderFavorite.created_at.desc(), OrderFavorite.id.desc())
    )
    return list(rows.scalars().all())
