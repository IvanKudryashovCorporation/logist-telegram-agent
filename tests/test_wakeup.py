"""Новый заказ будит воркеров уведомлений и геокодера сразу, а не через 30 секунд таймера."""

import asyncio

from app.services import wakeup
from app.telegram import work_group


async def test_signal_wakes_the_worker_at_once():
    wakeup.reset()
    stop = asyncio.Event()

    async def ping():
        await asyncio.sleep(0.05)
        wakeup.wake(wakeup.NOTIFY)

    asyncio.create_task(ping())
    started = asyncio.get_running_loop().time()
    woken = await wakeup.sleep_or_wake(wakeup.NOTIFY, stop, timeout=30, debounce=0.1)

    assert woken is True
    assert asyncio.get_running_loop().time() - started < 1  # не 30 секунд


async def test_timer_is_the_fallback_when_nothing_happens():
    wakeup.reset()

    woken = await wakeup.sleep_or_wake(wakeup.NOTIFY, asyncio.Event(), timeout=0.1)

    assert woken is False


async def test_burst_of_signals_gives_one_pass():
    wakeup.reset()
    stop = asyncio.Event()

    async def burst():
        for _ in range(5):
            wakeup.wake(wakeup.NOTIFY)
            await asyncio.sleep(0.02)

    asyncio.create_task(burst())
    assert await wakeup.sleep_or_wake(wakeup.NOTIFY, stop, timeout=5, debounce=0.3) is True
    # всё, что пришло за паузу, уже учтено: следующий сон ждёт новый сигнал, а не просыпается сразу
    assert await wakeup.sleep_or_wake(wakeup.NOTIFY, stop, timeout=0.1, debounce=0) is False


async def test_stop_event_ends_the_sleep():
    wakeup.reset()
    stop = asyncio.Event()
    stop.set()

    assert await wakeup.sleep_or_wake(wakeup.NOTIFY, stop, timeout=30) is False


async def test_saving_an_order_wakes_notifications_and_geocoder(monkeypatch):
    from decimal import Decimal

    from app.parsing.llm_parser import ParseResult
    from app.parsing.schema import ParsedOrder

    woken = []

    async def fake_parse_orders(text, **kwargs):
        return ParseResult(orders=[ParsedOrder(from_city="Гагра", to_city="Сочи", client_price=Decimal("2000"),
                                               pickup_time="12:00")])

    monkeypatch.setattr(work_group, "parse_orders", fake_parse_orders)
    monkeypatch.setattr(work_group.settings, "prefilter_enabled", False)
    monkeypatch.setattr(wakeup, "wake", lambda *names: woken.extend(names))

    ids = await work_group.upsert_order_text(chat_id=-100555, message_id=1, text="Гагра Сочи 2000 в 12:00 заявка",
                                             dispatcher_tg_id=1, dispatcher_username="disp")

    assert ids and set(woken) == {wakeup.NOTIFY, wakeup.GEOCODE}
