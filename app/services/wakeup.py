"""Будильник фоновых воркеров: «появился новый заказ — не ждите следующего круга таймера».

Уведомления водителям, геокодер и расчёт расстояний работают по таймеру (раз в 30 секунд), и заказ ждал своей очереди
до получаса… до целых 30 секунд. Агент после сохранения заказа будит воркеров, и они обрабатывают его сразу.
Таймер остаётся запасным вариантом: если сигнал потерялся, воркер всё равно проснётся сам.

Всё живёт в одном процессе (агент) и одном цикле событий, поэтому достаточно ``asyncio.Event``.
"""

import asyncio
from typing import Optional

#: Имена воркеров, которых можно будить.
NOTIFY = "notify"
GEOCODE = "geocode"
ROUTING = "routing"

_events: dict[str, asyncio.Event] = {}


def _event(name: str) -> asyncio.Event:
    event = _events.get(name)
    if event is None:
        event = _events[name] = asyncio.Event()
    return event


def wake(*names: str) -> None:
    """Будит воркеров. Безопасно вызывать откуда угодно в агенте; если воркер не запущен, сигнал просто не нужен."""
    for name in names:
        _event(name).set()


async def sleep_or_wake(
    name: str, stop_event: asyncio.Event, timeout: float, *, debounce: float = 1.0
) -> bool:
    """Спит до таймера, до остановки или до сигнала. ``True`` — разбудили сигналом.

    После сигнала ждёт ``debounce`` секунд: пачка заказов из одного всплеска даст один проход, а не десять."""
    event = _event(name)
    stop_task = asyncio.ensure_future(stop_event.wait())
    wake_task = asyncio.ensure_future(event.wait())
    try:
        done, _pending = await asyncio.wait({stop_task, wake_task}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in (stop_task, wake_task):
            if not task.done():
                task.cancel()
    if wake_task in done and not stop_event.is_set():
        if debounce > 0:
            await asyncio.sleep(debounce)
        event.clear()  # всё, что пришло за паузу, обработает этот же проход
        return True
    return False


def reset() -> None:
    """Для тестов: сбросить все сигналы."""
    _events.clear()
