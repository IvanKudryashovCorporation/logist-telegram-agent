"""Чтение публичных групп БЕЗ вступления: опрос истории вместо событий.

Telegram присылает события (новое сообщение, правка, удаление) только участникам
группы. Но у публичной группы с username историю может читать и тот, кто не вступал
(как в «предпросмотре» в приложении). Поэтому для групп с ``watch_only`` агент раз в
``WATCH_POLL_SECONDS`` секунд спрашивает историю и передаёт сообщения в ТОТ ЖЕ разбор,
что и события: дубли, геокодер, цены, очередь повторного разбора.

Что опрос видит:

* новые сообщения — все, что появились после прошлого опроса (в первый раз — за последние
  ``WATCH_INITIAL_HOURS`` часов, чтобы лента сразу наполнилась);
* правки — сообщения, изменённые за последние несколько опросов;
* удаления — заявки этой группы, чьих сообщений в истории больше нет.

Закрытые группы (по пригласительной ссылке) и группы со скрытой для не участников
историей так не читаются: для них остаётся вступление.
"""

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable, Optional

from sqlalchemy import select, update
from telethon.errors import FloodWaitError

from app.config import settings
from app.db.base import SessionLocal
from app.models import HIDDEN_STATUSES, Order, WorkGroup
from app.telegram.work_group import _handle_deleted, _handle_message

log = logging.getLogger("agent.watcher")

#: Сколько последних сообщений просматриваем в первый раз / за один опрос.
INITIAL_LIMIT = 200
NEW_LIMIT = 300
#: Правки ищем среди стольких последних сообщений.
EDIT_WINDOW = 40
#: Сколько последних кругов считается «недавней правкой».
EDIT_POLLS = 3
#: Пауза между группами одного аккаунта, секунд: не частим запросами.
GROUP_GAP = 1.0


@dataclass(frozen=True)
class WatchGroup:
    id: int
    tg_chat_id: int
    title: str
    username: Optional[str]
    session_name: Optional[str]
    last_message_id: Optional[int]


async def watch_groups() -> list[WatchGroup]:
    """Активные группы в режиме «наблюдение без вступления»."""
    async with SessionLocal() as session:
        rows = (
            await session.execute(
                select(WorkGroup)
                .where(WorkGroup.is_active.is_(True), WorkGroup.watch_only.is_(True))
                .order_by(WorkGroup.id)
            )
        ).scalars().all()
        return [
            WatchGroup(g.id, g.tg_chat_id, g.title, g.username, g.session_name or None, g.last_message_id)
            for g in rows
        ]


async def _save_last_id(group_id: int, last_id: int) -> None:
    async with SessionLocal() as session:
        await session.execute(
            update(WorkGroup).where(WorkGroup.id == group_id).values(last_message_id=last_id)
        )
        await session.commit()


async def _recent_message_ids(chat_id: int, since: datetime) -> list[int]:
    """id сообщений живых заявок этой группы (для поиска удалённых)."""
    async with SessionLocal() as session:
        ids = (
            await session.execute(
                select(Order.source_message_id)
                .where(
                    Order.source_chat_id == chat_id,
                    Order.status.notin_(HIDDEN_STATUSES),
                    Order.created_at >= since.replace(tzinfo=None),
                )
                .distinct()
            )
        ).scalars().all()
    return sorted(ids)[-200:]


async def poll_group(
    client,
    group: WatchGroup,
    *,
    handle: Callable[..., Awaitable] = _handle_message,
    handle_deleted: Callable[..., Awaitable] = _handle_deleted,
    now: Optional[datetime] = None,
    check_changes: bool = True,
) -> dict[str, int]:
    """Один опрос группы. Возвращает счётчики ``new`` / ``edited`` / ``deleted``.

    ``check_changes=False`` — только новые сообщения: правки и удаления при сотне групп
    проверяются реже (каждый ``WATCH_SLOW_EVERY``-й круг), чтобы не плодить запросы."""
    now = now or datetime.now(timezone.utc)
    entity = await client.get_entity(group.username or group.tg_chat_id)
    last = group.last_message_id

    if last is None:
        # Первый опрос: берём недавнее, но дальше следим только за новым.
        recent = await client.get_messages(entity, limit=INITIAL_LIMIT)
        cutoff = now - timedelta(hours=max(1, settings.watch_initial_hours))
        new = sorted((m for m in recent if m.date >= cutoff), key=lambda m: m.id)
        newest = max((m.id for m in recent), default=0)
    else:
        found = await client.get_messages(entity, limit=NEW_LIMIT, min_id=last)
        new = sorted(found, key=lambda m: m.id)
        newest = max((m.id for m in new), default=last)

    for message in new:
        await handle(message, False)

    # Правки: сообщение, изменённое за последние несколько опросов.
    edited = 0
    if last is not None and check_changes:
        edit_since = now - timedelta(
            seconds=max(15, settings.watch_poll_seconds) * max(1, settings.watch_slow_every) * EDIT_POLLS
        )
        for message in await client.get_messages(entity, limit=EDIT_WINDOW):
            stamp = getattr(message, "edit_date", None)
            if stamp is not None and message.id <= last and stamp >= edit_since:
                await handle(message, True)
                edited += 1

    # Удаления: заявки, чьих сообщений уже нет в истории.
    deleted = 0
    ids = (
        await _recent_message_ids(
            group.tg_chat_id, now - timedelta(hours=max(1, settings.asap_expire_hours) * 2)
        )
        if check_changes
        else []
    )
    if ids:
        fetched = await client.get_messages(entity, ids=ids)
        missing = [i for i, m in zip(ids, fetched, strict=True) if m is None]
        if missing:
            await handle_deleted(group.tg_chat_id, missing)
            deleted = len(missing)

    if newest and newest != last:
        await _save_last_id(group.id, newest)
    return {"new": len(new), "edited": edited, "deleted": deleted}


def split_by_session(groups: list[WatchGroup]) -> dict[Optional[str], list[WatchGroup]]:
    """Группы по аккаунтам: каждый аккаунт опрашивается своей очередью, параллельно с другими."""
    grouped: dict[Optional[str], list[WatchGroup]] = {}
    for group in groups:
        grouped.setdefault(group.session_name, []).append(group)
    return grouped


async def _poll_session(client, groups: list[WatchGroup], cycle: int, stop_event: asyncio.Event) -> None:
    """Один круг по группам одного аккаунта: по очереди, с паузой между группами."""
    check_changes = cycle % max(1, settings.watch_slow_every) == 0
    for group in groups:
        if stop_event.is_set():
            return
        try:
            counts = await poll_group(client, group, check_changes=check_changes)
            if any(counts.values()):
                log.info("«%s»: %s", group.title, counts)
        except FloodWaitError as exc:
            log.warning("FloodWait %s с на «%s» — этот аккаунт ждёт", exc.seconds, group.title)
            await asyncio.sleep(exc.seconds + 1)
        except Exception:
            log.exception("Ошибка опроса группы «%s»", group.title)
        await asyncio.sleep(GROUP_GAP)


async def run_watch_worker(clients: dict, stop_event: asyncio.Event) -> None:
    """Фоновый цикл опроса групп без вступления. ``clients`` — {имя сессии | None: клиент}."""
    interval = max(15, settings.watch_poll_seconds)
    log.info("Наблюдение за группами без вступления: круг не чаще раза в %s с", interval)
    cycle = 0
    while not stop_event.is_set():
        started = asyncio.get_running_loop().time()
        try:
            groups = await watch_groups()
        except Exception:
            log.exception("Не удалось получить список групп для наблюдения")
            groups = []
        queues = []
        for name, session_groups in split_by_session(groups).items():
            client = clients.get(name)
            if client is None:
                log.warning("Нет клиента для сессии %s — групп пропущено: %s", name, len(session_groups))
                continue
            queues.append(_poll_session(client, session_groups, cycle, stop_event))
        if queues:
            await asyncio.gather(*queues)
        cycle += 1
        pause = max(0.0, interval - (asyncio.get_running_loop().time() - started))
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=pause)
        except asyncio.TimeoutError:
            continue
