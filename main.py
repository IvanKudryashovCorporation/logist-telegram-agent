"""Точка входа агента.

Слушает рабочие группы диспетчеров и разбирает заявки в БД — это агрегатор
заказов для водителей (см. app/web/), никакой переписки или публикации
агент больше не ведёт.

Кроме самого слушателя процесс поднимает фоновые задачи:

* воркер очереди повторного разбора (:mod:`app.telegram.queue_worker`) —
  добирает сообщения, которые не удалось разобрать из-за лимита/сбоя LLM;
* цикл обслуживания (:mod:`app.services.cleanup`) — протухание заявок и чистка
  старых метрик;
* уведомления владельца (:mod:`app.services.notify`) — алерты при сериях ошибок.

Все они останавливаются через общий ``stop_event`` в ``finally``, чтобы по
Ctrl+C не оставалось висящих задач и незакрытой сессии Telegram.
"""

import asyncio
import logging

from sqlalchemy import select

from app.config import settings
from app.db.base import SessionLocal
from app.models import WorkGroup
from app.services import notifier, run_cleanup_loop, run_geocode_worker
from app.telegram.client import build_client
from app.telegram.queue_worker import run_queue_worker
from app.telegram.work_group import register_work_group_handlers

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s %(name)s: %(message)s",
)
log = logging.getLogger("agent")


async def _bootstrap_work_group(client) -> None:
    """Разовый перенос группы из .env (старый способ настройки) в БД.

    Дальше список групп живёт только в БД через ``scripts.manage_work_groups``,
    .env для этого больше не используется.
    """
    if not settings.work_group_chat_id:
        return
    async with SessionLocal() as session:
        existing = (
            await session.execute(
                select(WorkGroup).where(WorkGroup.tg_chat_id == settings.work_group_chat_id)
            )
        ).scalar_one_or_none()
        if existing is not None:
            return
        entity = await client.get_entity(settings.work_group_chat_id)
        session.add(
            WorkGroup(
                tg_chat_id=settings.work_group_chat_id,
                title=getattr(entity, "title", str(settings.work_group_chat_id)),
            )
        )
        await session.commit()
    log.info("Перенёс WORK_GROUP_CHAT_ID из .env в список рабочих групп.")


async def _active_work_group_ids() -> list[int]:
    async with SessionLocal() as session:
        return list(
            (
                await session.execute(
                    select(WorkGroup.tg_chat_id).where(WorkGroup.is_active.is_(True))
                )
            ).scalars().all()
        )


def _start_background(client, stop_event: asyncio.Event) -> list[asyncio.Task]:
    """Запускает фоновые задачи. Каждая сама переживает свои ошибки."""
    tasks: list[asyncio.Task] = [
        asyncio.create_task(run_cleanup_loop(stop_event), name="cleanup"),
        asyncio.create_task(run_geocode_worker(stop_event), name="geocode"),
    ]
    if settings.queue_enabled:
        tasks.append(
            asyncio.create_task(run_queue_worker(client, stop_event), name="queue_worker")
        )
    else:
        log.warning(
            "Очередь повторного разбора выключена (QUEUE_ENABLED=false): "
            "сообщения со сбоем LLM будут теряться."
        )
    return tasks


async def main() -> None:
    client = build_client()
    await client.start()

    me = await client.get_me()
    log.info("Агент запущен от лица %s (id=%s)", me.first_name, me.id)

    await _bootstrap_work_group(client)
    work_group_ids = await _active_work_group_ids()

    if not work_group_ids:
        log.warning(
            "Нет ни одной активной рабочей группы — заявки читать неоткуда. "
            "Добавьте: python -m scripts.manage_work_groups add <chat_id_или_@username>"
        )
    else:
        register_work_group_handlers(client, list(work_group_ids))
        log.info("Слушаю заявки в %s рабочих группах.", len(work_group_ids))

    # Уведомления владельца ходят через тот же Telethon-клиент, поэтому
    # подключаются только после client.start().
    notifier.attach(client)
    if notifier.enabled and settings.notify_on_start:
        await notifier.send(
            "Агент запущен.\n"
            f"Рабочих групп: {len(work_group_ids)}\n"
            f"Очередь разбора: {'вкл' if settings.queue_enabled else 'выкл'}"
        )

    stop_event = asyncio.Event()
    background = _start_background(client, stop_event)

    log.info("Ожидание событий. Ctrl+C для остановки.")
    try:
        await client.run_until_disconnected()
    finally:
        stop_event.set()
        for task in background:
            task.cancel()
        # Ждём отмены всех задач, чтобы по Ctrl+C не оставалось «висяков»
        # и незакрытого соединения. Исключения глотим — мы уже выходим.
        await asyncio.gather(*background, return_exceptions=True)
        notifier.detach()
        log.info("Фоновые задачи остановлены.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Остановлено пользователем.")
