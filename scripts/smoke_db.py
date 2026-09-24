"""Проверка, что слой БД работает: создаёт тестовый заказ, читает и удаляет его.

    python -m scripts.smoke_db
"""

import asyncio
from datetime import datetime
from decimal import Decimal

from sqlalchemy import select

from app.db.base import SessionLocal
from app.models import ActionLog, ActorType, Order, OrderStatus


async def main() -> None:
    async with SessionLocal() as session:
        order = Order(
            source_chat_id=-100123456789,
            source_message_id=1,
            dispatcher_tg_id=111,
            dispatcher_username="test_dispatcher",
            raw_text="24.05 18:00 Севастополь — Сочи, 2 пассажира, 14000, Иван +79990000000",
            pickup_at=datetime(2026, 5, 24, 18, 0),
            from_address="Севастополь, ул. Ленина 1",
            to_address="Сочи, аэропорт",
            from_city="Севастополь",
            to_city="Сочи",
            passengers=2,
            client_name="Иван",
            client_phone="+79990000000",
            client_price=Decimal("14000"),
            driver_payment=Decimal("14000"),  # 1 в 1, без наценки
            status=OrderStatus.NEW,
        )
        session.add(order)
        await session.flush()

        session.add(
            ActionLog(
                order_id=order.id,
                actor=ActorType.AGENT,
                action="order_created",
                details="создан из smoke-теста",
            )
        )
        await session.commit()
        order_id = order.id

    async with SessionLocal() as session:
        loaded = (await session.execute(select(Order).where(Order.id == order_id))).scalar_one()
        print(f"Прочитан: {loaded!r}")
        print(f"  подача:  {loaded.pickup_at}")
        print(f"  цена:    {loaded.client_price} ₽")

        logs = (await session.execute(select(ActionLog).where(ActionLog.order_id == order_id))).scalars().all()
        print(f"  история: {[log.action for log in logs]}")

        await session.delete(loaded)
        await session.commit()
        print("\nТестовые данные удалены. Слой БД работает.")


if __name__ == "__main__":
    asyncio.run(main())
