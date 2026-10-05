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
from app.services import (
    notifier,
    run_cleanup_loop,
    run_geocode_worker,
    run_routing_worker,
    run_subscription_worker,
)
from app.telegram.accounts import active_groups_by_session
from app.telegram.client import build_client
from app.telegram.queue_worker import run_queue_worker
from app.telegram.watcher import run_watch_worker, watch_groups
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


async def _start_extra_client(session_name: str):
    """Поднимает клиент дополнительного аккаунта. Без интерактивной авторизации:
    если сессия не авторизована, службе нечего спрашивать — пропускаем её группы."""
    extra = build_client(session_name)
    await extra.connect()
    if not await extra.is_user_authorized():
        log.error(
            "Сессия %s не авторизована (scripts.auth_account) — её группы пропущены.", session_name
        )
        await extra.disconnect()
        return None
    me = await extra.get_me()
    log.info("Дополнительный аккаунт %s: %s (id=%s)", session_name, me.first_name, me.id)
    return extra


def _start_background(client, stop_event: asyncio.Event, client_for=None) -> list[asyncio.Task]:
    """Запускает фоновые задачи. Каждая сама переживает свои ошибки."""
    tasks: list[asyncio.Task] = [
        asyncio.create_task(run_cleanup_loop(stop_event), name="cleanup"),
        asyncio.create_task(run_geocode_worker(stop_event), name="geocode"),
        asyncio.create_task(run_routing_worker(stop_event), name="routing"),
        asyncio.create_task(run_subscription_worker(stop_event), name="subscriptions"),
    ]
    if settings.queue_enabled:
        tasks.append(
            asyncio.create_task(
                run_queue_worker(client, stop_event, client_for=client_for), name="queue_worker"
            )
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
    groups = await active_groups_by_session()

    clients = {None: client}
    chat_clients: dict[int, object] = {}
    for session_name, chat_ids in groups.items():
        owner = clients.get(session_name)
        if owner is None:
            owner = await _start_extra_client(session_name)
            if owner is None:
                continue
            clients[session_name] = owner
        register_work_group_handlers(owner, list(chat_ids))
        chat_clients.update({chat_id: owner for chat_id in chat_ids})

    # Группы «наблюдение без вступления» читаются опросом, но клиент для их сессии нужен так же.
    watching = await watch_groups()
    for group in watching:
        if group.session_name not in clients:
            extra = await _start_extra_client(group.session_name)
            if extra is not None:
                clients[group.session_name] = extra

    if not chat_clients and not watching:
        log.warning(
            "Нет ни одной активной рабочей группы — заявки читать неоткуда. "
            "Добавьте: python -m scripts.manage_work_groups add <chat_id_или_@username>"
        )
    else:
        log.info(
            "Слушаю заявки в %s рабочих группах (аккаунтов: %s).", len(chat_clients), len(clients)
        )
    if watching:
        log.info("Без вступления читаем (опросом) групп: %s", len(watching))
    work_group_ids = list(chat_clients) + [g.tg_chat_id for g in watching]

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
    background = _start_background(
        client, stop_event, client_for=lambda chat_id: chat_clients.get(chat_id, client)
    )
    if watching:
        background.append(asyncio.create_task(run_watch_worker(clients, stop_event), name="watcher"))

    log.info("Ожидание событий. Ctrl+C для остановки.")
    try:
        await asyncio.gather(*(c.run_until_disconnected() for c in clients.values()))
    finally:
        stop_event.set()
        for task in background:
            task.cancel()
        # Ждём отмены всех задач, чтобы по Ctrl+C не оставалось «висяков»
        # и незакрытого соединения. Исключения глотим — мы уже выходим.
        await asyncio.gather(*background, return_exceptions=True)
        notifier.detach()
        for extra_name, extra_client in clients.items():
            if extra_name is not None:
                await extra_client.disconnect()
        log.info("Фоновые задачи остановлены.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Остановлено пользователем.")
