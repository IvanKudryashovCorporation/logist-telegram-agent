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
import contextlib
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable, Optional

from sqlalchemy import func, select, update
from telethon.errors import FloodWaitError

from app.config import settings
from app.db.base import SessionLocal
from app.models import HIDDEN_STATUSES, Order, ParseStat, WorkGroup
from app.telegram.work_group import _handle_deleted, _handle_message

log = logging.getLogger("agent.watcher")

#: Сколько последних сообщений просматриваем в первый раз / за один опрос.
INITIAL_LIMIT = 200
NEW_LIMIT = 300
#: Правки ищем среди стольких последних сообщений.
EDIT_WINDOW = 40
#: Сколько последних кругов считается «недавней правкой».
EDIT_POLLS = 3
#: Пауза после опроса группы, секунд: не частим запросами (групп опрашивается несколько одновременно).
GROUP_GAP = 0.15
#: Как часто пересчитываем активность групп (по журналу разбора), секунд.
ACTIVITY_REFRESH_SECONDS = 300

@dataclass(frozen=True)
class WatchGroup:
    id: int
    tg_chat_id: int
    title: str
    username: Optional[str]
    session_name: Optional[str]
    last_message_id: Optional[int]


def _entities_of(client) -> dict:
    """Кэш определённых чатов лежит на самом клиенте: имя группы определяем один раз, а не на каждом круге.

    Запрос имени (ResolveUsername) — самый дорогой и первым упирается в лимит Telegram."""
    cache = getattr(client, "_watch_entities", None)
    if cache is None:
        cache = {}
        with contextlib.suppress(AttributeError):
            client._watch_entities = cache
    return cache


async def _entity(client, group: "WatchGroup"):
    cache = _entities_of(client)
    entity = cache.get(group.id)
    if entity is None:
        entity = await client.get_entity(group.username or group.tg_chat_id)
        cache[group.id] = entity
    return entity


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
    entity = await _entity(client, group)
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


async def activity_by_chat(hours: int, *, now: Optional[datetime] = None) -> dict[int, float]:
    """Сообщений в час по каждой группе за последние ``hours`` часов (по журналу разбора)."""
    hours = max(1, hours)
    since = (now or datetime.now(timezone.utc)).replace(tzinfo=None) - timedelta(hours=hours)
    async with SessionLocal() as session:
        rows = (
            await session.execute(
                select(ParseStat.chat_id, func.count())
                .where(ParseStat.created_at >= since, ParseStat.chat_id.is_not(None))
                .group_by(ParseStat.chat_id)
            )
        ).all()
    return {chat_id: count / hours for chat_id, count in rows}


def poll_every(per_hour: float) -> int:
    """Раз в сколько кругов опрашивать группу с такой активностью: загруженные — каждый круг, тихие — реже."""
    if per_hour >= settings.watch_hot_per_hour:
        return 1
    if per_hour >= settings.watch_warm_per_hour:
        return max(1, settings.watch_warm_every)
    return max(1, settings.watch_cold_every)


def schedule(groups: list[WatchGroup], cycle: int, activity: dict[int, float]) -> dict[int, int]:
    """Какие группы опрашиваем в этом круге: ``{id группы: раз в сколько кругов её опрашивают}``.

    Тихие группы раскладываются по разным кругам (по номеру группы), чтобы нагрузка была ровной, а не волной
    раз в несколько кругов. Группа без истории (ни разу не опрошена) идёт сразу."""
    if not settings.watch_adaptive:
        return {group.id: 1 for group in groups}
    due: dict[int, int] = {}
    for group in groups:
        every = poll_every(activity.get(group.tg_chat_id, 0.0))
        if group.last_message_id is None or every == 1 or (cycle + group.id) % every == 0:
            due[group.id] = every
    return due


def split_by_session(groups: list[WatchGroup]) -> dict[Optional[str], list[WatchGroup]]:
    """Группы по аккаунтам: каждый аккаунт опрашивается своей очередью, параллельно с другими."""
    grouped: dict[Optional[str], list[WatchGroup]] = {}
    for group in groups:
        grouped.setdefault(group.session_name, []).append(group)
    return grouped


async def _poll_session(
    client, groups: list[WatchGroup], cycle: int, stop_event: asyncio.Event, every: Optional[dict[int, int]] = None
) -> None:
    """Один круг по группам одного аккаунта: по ``watch_concurrency`` групп одновременно.

    Лимит Telegram (FloodWait) касается всего аккаунта: остальные группы ждут вместе с той, где он случился."""
    check_changes = cycle % max(1, settings.watch_slow_every) == 0
    gate = asyncio.Semaphore(max(1, settings.watch_concurrency))
    loop = asyncio.get_running_loop()
    blocked_until = 0.0

    async def poll_one(group: WatchGroup) -> None:
        nonlocal blocked_until
        if stop_event.is_set():
            return
        async with gate:
            wait = blocked_until - loop.time()
            if wait > 0:
                await asyncio.sleep(wait)
            try:
                # Редко опрашиваемые группы проверяем на правки и удаления при каждом опросе: иначе правка
                # успела бы выйти из окна поиска раньше следующей проверки.
                rare = (every or {}).get(group.id, 1) > 1
                counts = await poll_group(client, group, check_changes=check_changes or rare)
                if any(counts.values()):
                    log.info("«%s»: %s", group.title, counts)
            except FloodWaitError as exc:
                log.warning("FloodWait %s с на «%s» — этот аккаунт ждёт", exc.seconds, group.title)
                blocked_until = max(blocked_until, loop.time() + exc.seconds + 1)
            except Exception:
                _entities_of(client).pop(group.id, None)  # вдруг сменилось имя или доступ: определим заново
                log.exception("Ошибка опроса группы «%s»", group.title)
            await asyncio.sleep(GROUP_GAP)

    await asyncio.gather(*(poll_one(group) for group in groups))


async def run_watch_worker(clients: dict, stop_event: asyncio.Event) -> None:
    """Фоновый цикл опроса групп без вступления. ``clients`` — {имя сессии | None: клиент}."""
    interval = max(15, settings.watch_poll_seconds)
    log.info("Наблюдение за группами без вступления: круг не чаще раза в %s с", interval)
    cycle = 0
    activity: dict[int, float] = {}
    activity_at = float("-inf")
    while not stop_event.is_set():
        started = asyncio.get_running_loop().time()
        try:
            groups = await watch_groups()
        except Exception:
            log.exception("Не удалось получить список групп для наблюдения")
            groups = []
        loop_time = asyncio.get_running_loop().time()
        if settings.watch_adaptive and loop_time - activity_at > ACTIVITY_REFRESH_SECONDS:
            try:
                activity = await activity_by_chat(settings.watch_activity_hours)
                activity_at = loop_time
            except Exception:
                log.exception("Не удалось посчитать активность групп: опрашиваем все группы каждый круг")
                activity, activity_at = {}, loop_time
        due = schedule(groups, cycle, activity)
        total = len(groups)
        groups = [group for group in groups if group.id in due]
        queues = []
        for name, session_groups in split_by_session(groups).items():
            client = clients.get(name)
            if client is None:
                log.warning("Нет клиента для сессии %s — групп пропущено: %s", name, len(session_groups))
                continue
            queues.append(_poll_session(client, session_groups, cycle, stop_event, due))
        if queues:
            await asyncio.gather(*queues)
        cycle += 1
        spent = asyncio.get_running_loop().time() - started
        if spent > interval * 2 or cycle % 90 == 0:
            log.info("Круг опроса №%s: %s из %s групп за %.1f с (интервал %s с)", cycle, len(groups), total, spent, interval)
        pause = max(0.0, interval - spent)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=pause)
        except asyncio.TimeoutError:
            continue
