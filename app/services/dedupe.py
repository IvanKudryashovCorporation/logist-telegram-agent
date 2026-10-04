"""Фоновая уборка повторов: отменяет живые заявки, которые на самом деле — та же заявка.

Основная защита стоит при приёме сообщения (:func:`app.telegram.work_group._find_duplicate`),
но повтор может проскочить: диспетчер определился позже, город написан иначе, правило
улучшили. Этот проход раз в цикл обслуживания находит такие пары и оставляет самую
новую заявку (в ней актуальная цена), а старые отменяет.
"""

import logging

from sqlalchemy import select, update

from app.db.base import SessionLocal
from app.dedupe_rules import LiveOrder, duplicate_ids
from app.models import ActionLog, ActorType, Order, OrderStatus

log = logging.getLogger("app.dedupe")

_LIVE = (OrderStatus.NEW, OrderStatus.NEEDS_CLARIFICATION)


async def cancel_duplicate_orders() -> int:
    """Отменяет повторы среди живых заявок ленты. Возвращает, сколько отменено."""
    async with SessionLocal() as session:
        rows = (
            await session.execute(
                select(Order).where(Order.status.in_(_LIVE), Order.taken_by_token.is_(None))
            )
        ).scalars().all()
        live = [
            LiveOrder(
                id=o.id, tg_id=o.dispatcher_tg_id, username=o.dispatcher_username,
                from_city=o.from_city, to_city=o.to_city, pickup_at=o.pickup_at,
                phone=o.client_phone, passengers=o.passengers, created_at=o.created_at,
                raw_text=o.raw_text, from_address=o.from_address, to_address=o.to_address,
            )
            for o in rows
        ]
        ids = duplicate_ids(live)
        if not ids:
            return 0
        await session.execute(
            update(Order)
            .where(Order.id.in_(ids))
            .values(status=OrderStatus.CANCELLED)
            .execution_options(synchronize_session=False)
        )
        for order_id in ids:
            session.add(
                ActionLog(
                    order_id=order_id, actor=ActorType.SYSTEM,
                    action="duplicate_cancelled", details="повтор более новой заявки",
                )
            )
        await session.commit()
        log.info("Отменено повторов заявок: %s (%s)", len(ids), ids)
        return len(ids)
