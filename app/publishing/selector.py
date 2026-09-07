"""Подбор водительских групп под маршрут заказа (вопрос 42)."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import DriverGroup, Order


async def select_groups(session: AsyncSession, order: Order) -> list[DriverGroup]:
    """Группы без направлений — общие, подходят всем. Иначе ищем совпадение города."""
    groups = (
        (await session.execute(select(DriverGroup).where(DriverGroup.is_active.is_(True))))
        .scalars()
        .all()
    )

    cities = {c.lower() for c in (order.from_city, order.to_city) if c}
    if not cities:
        return [g for g in groups if not g.directions]

    matched = []
    for group in groups:
        if not group.directions:
            matched.append(group)
            continue
        directions = [d.strip().lower() for d in group.directions.split(",") if d.strip()]
        if any(city in d or d in city for city in cities for d in directions):
            matched.append(group)
    return matched
