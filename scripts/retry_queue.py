"""Повторный разбор сообщений, застрявших в очереди.

    python -m scripts.retry_queue                 # показать, что не разобрано
    python -m scripts.retry_queue --run           # сбросить попытки и разобрать
    python -m scripts.retry_queue --run --limit 5
    python -m scripts.retry_queue --status pending --run

В норме это делает фоновый воркер (``app/telegram/queue_worker.py``) внутри
``main.py``. Скрипт нужен для двух случаев:

* агент не запущен, а очередь разобрать хочется сейчас;
* сообщения дошли до ``failed`` (попытки исчерпаны) — воркер их больше не
  трогает, и нужен человек: устранить причину (квота LLM, ключ, сеть),
  затем ``--run``. Тот же сброс доступен кнопкой в админке ``/admin/queue``.

Как и ``scripts.backfill_orders``, требует файл сессии Telegram, поэтому НЕ
запускайте параллельно с работающим ``main.py`` — сначала остановите агента.
"""

import argparse
import asyncio
import logging

from sqlalchemy import select, update

from app.db.base import SessionLocal
from app.models import PendingMessage, PendingStatus
from app.telegram.client import build_client
from app.telegram.pending import failed_messages, queue_depth
from app.telegram.queue_worker import process_one
from app.timeutil import now_utc_naive

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-7s %(name)s: %(message)s")
log = logging.getLogger("scripts.retry_queue")


async def _targets(status: str, limit: int) -> list[PendingMessage]:
    """Что будем показывать/повторять."""
    if status == "failed":
        return await failed_messages(limit=limit)
    async with SessionLocal() as session:
        return list(
            (
                await session.execute(
                    select(PendingMessage)
                    .where(PendingMessage.status == PendingStatus(status))
                    .order_by(PendingMessage.id.desc())
                    .limit(limit)
                )
            ).scalars().all()
        )


def _print(items: list[PendingMessage]) -> None:
    if not items:
        print("Очередь пуста — всё разобрано.")
        return
    for item in items:
        when = item.next_attempt_at.strftime("%d.%m %H:%M") if item.next_attempt_at else "—"
        print(f"\n#{item.id}  chat={item.chat_id} msg={item.message_id} "
              f"статус={item.status.value} попыток={item.attempts} след.{when}")
        if item.last_error:
            print(f"  ошибка: {item.last_error[:200]}")
        print(f"  текст:  {item.text[:200]!r}")


async def _reset(ids: list[int]) -> None:
    """Обнуляет попытки и ставит срок на сейчас — иначе воркер их не подхватит."""
    async with SessionLocal() as session:
        await session.execute(
            update(PendingMessage)
            .where(PendingMessage.id.in_(ids))
            .values(
                status=PendingStatus.PENDING,
                attempts=0,
                next_attempt_at=now_utc_naive(),
                last_error="reset_by_retry_queue",
            )
        )
        await session.commit()


async def main(status: str, limit: int, run: bool) -> int:
    depth = await queue_depth()
    print("Состояние очереди:", depth or "пусто")

    items = await _targets(status, limit)
    _print(items)

    if not items:
        return 0
    if not run:
        print("\nЭто сухой просмотр. Для повтора: python -m scripts.retry_queue --run")
        return 0

    ids = [item.id for item in items]
    await _reset(ids)
    print(f"\nСброшено попыток у {len(ids)} сообщений, разбираю заново…")

    client = build_client()
    await client.start()
    ok = 0
    try:
        for pending_id in ids:
            if await process_one(client, pending_id):
                ok += 1
                print(f"  #{pending_id} разобрано")
            else:
                print(f"  #{pending_id} снова не удалось (см. last_error)")
    finally:
        await client.disconnect()

    print(f"\nГотово: {ok} из {len(ids)} разобрано.")
    print("Состояние очереди:", await queue_depth())
    return 0 if ok == len(ids) else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--status", choices=["failed", "pending", "done"], default="failed",
                        help="какое состояние очереди брать (по умолчанию failed)")
    parser.add_argument("--limit", type=int, default=50, help="сколько сообщений взять")
    parser.add_argument("--run", action="store_true",
                        help="реально повторить разбор (без флага — только просмотр)")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(main(args.status, args.limit, args.run)))
