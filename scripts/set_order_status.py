"""Временный инструмент для теста Этапа 2 — вручную поменять статус заказа.

Полноценный флоу назначения появится на Этапе 4; здесь только то, что нужно
для проверки автоудаления публикаций (вопросы 50-51):

    python -m scripts.set_order_status <order_id> assigned
    python -m scripts.set_order_status <order_id> cancelled
"""

import argparse
import asyncio

from app.db.base import SessionLocal
from app.models import Order, OrderStatus
from app.telegram.client import build_client
from app.publishing.service import delete_publications


async def main(order_id: int, new_status: str) -> None:
    status_map = {
        "assigned": OrderStatus.DRIVER_ASSIGNED,
        "cancelled": OrderStatus.CANCELLED,
    }
    status = status_map[new_status]

    async with SessionLocal() as session:
        order = await session.get(Order, order_id)
        if order is None:
            print("Заказ не найден.")
            return
        order.status = status
        await session.commit()

    client = build_client()
    await client.start()
    await delete_publications(order_id, client, reason=new_status)
    await client.disconnect()
    print(f"Заказ #{order_id} -> {status.value}, публикации удалены.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("order_id", type=int)
    parser.add_argument("new_status", choices=["assigned", "cancelled"])
    args = parser.parse_args()
    asyncio.run(main(args.order_id, args.new_status))
