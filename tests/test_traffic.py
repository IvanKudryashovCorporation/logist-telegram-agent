"""Источники заходов на сайт: разбор Referer/меток, фильтр роботов, запись и отчёт для /admin/traffic."""

import asyncio

import pytest
from sqlalchemy import select

from app.db.base import SessionLocal
from app.models import SiteVisit
from app.services import reporting
from app.web import traffic

OWN = "lentazakazov.ru"
CHROME = "Mozilla/5.0 (Windows NT 10.0) AppleWebKit/537.36 Chrome/120 Safari/537.36"
PHONE = "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 Chrome/120 Mobile Safari/537.36"


def _classify(referer="", query=None, ua=CHROME):
    return traffic.classify(referer=referer, query=query or {}, user_agent=ua, own_host=OWN)


@pytest.mark.parametrize(
    "referer, source",
    [
        ("https://yandex.ru/search/?text=x", "yandex"),
        ("https://www.google.com/", "google"),
        ("https://t.me/somechannel/12", "telegram"),
        ("https://away.vk.com/away.php?to=x", "vk"),
        ("https://m.vk.com/", "vk"),
        ("https://some-blog.example/post", "site"),
        ("", "direct"),
    ],
)
def test_source_from_referer(referer, source):
    assert _classify(referer)["source"] == source


def test_referer_keeps_only_host():
    info = _classify("https://www.some-blog.example/post?id=5#x")
    assert info["referrer_host"] == "some-blog.example"


def test_internal_navigation_is_not_a_visit():
    assert _classify("https://lentazakazov.ru/orders/5") is None
    assert _classify("https://www.lentazakazov.ru/") is None


def test_notlookalike_host_is_not_internal():
    assert _classify("https://evil-lentazakazov.ru/")["source"] == "site"


def test_utm_and_src_tags_beat_referer():
    info = _classify("https://t.me/x", {"utm_source": "Chat42", "utm_medium": "Post", "utm_campaign": "Oct"})
    assert info["source"] == "utm:chat42"
    assert (info["utm_medium"], info["utm_campaign"]) == ("post", "oct")
    assert _classify("", {"src": "flyer"})["source"] == "utm:flyer"


def test_pwa_shortcut_and_telegram_browser():
    assert _classify("", {"src": "pwa"})["source"] == "pwa"
    assert _classify("", ua=CHROME + " Telegram-Android/10.0")["source"] == "telegram"


def test_bots_and_empty_agents_are_ignored():
    assert traffic.is_bot("")
    assert traffic.is_bot("Mozilla/5.0 (compatible; YandexBot/3.0)")
    assert traffic.is_bot("python-httpx/0.27")
    assert not traffic.is_bot(CHROME)


def test_device_detection():
    assert traffic.device_of(PHONE) == "mobile"
    assert traffic.device_of(CHROME) == "desktop"
    assert traffic.device_of(CHROME + " Telegram-Android/10") == "telegram"


def test_hashes_depend_on_day_and_hide_ip():
    a, ip_a = traffic.visitor_hashes("1.2.3.4", CHROME, day="20261021")
    b, ip_b = traffic.visitor_hashes("1.2.3.4", CHROME, day="20261022")
    c, ip_c = traffic.visitor_hashes("1.2.3.4", PHONE, day="20261021")
    assert a != b and ip_a != ip_b
    assert ip_a == ip_c and a != c  # тот же IP в другом браузере — тот же ip_hash
    assert "1.2.3.4" not in a + ip_a


def test_session_window():
    traffic._last_seen.clear()
    assert traffic.is_new_session("v", None, now=1000.0)
    assert not traffic.is_new_session("v", None, now=1100.0)
    assert traffic.is_new_session("v", 7, now=1100.0)  # вошедший — отдельный сеанс
    assert traffic.is_new_session("v", None, now=1000.0 + traffic.SESSION_GAP_SECONDS)


async def test_page_view_records_one_visit_per_session(client):
    traffic._last_seen.clear()
    headers = {"accept": "text/html", "user-agent": PHONE, "referer": "https://yandex.ru/search?text=a"}

    await client.get("/about", headers=headers)
    await client.get("/about", headers=headers)
    await client.get("/about", headers={"accept": "application/json", "user-agent": PHONE})
    await client.get("/about", headers={"accept": "text/html", "user-agent": "Googlebot/2.1"})
    await asyncio.sleep(0.3)  # запись захода идёт фоновой задачей

    async with SessionLocal() as session:
        rows = (await session.execute(select(SiteVisit))).scalars().all()
    assert len(rows) == 1
    assert rows[0].source == "yandex" and rows[0].landing == "/about" and rows[0].device == "mobile"


async def test_report_counts_sources_and_attributes_signups(session):
    from datetime import timedelta

    from app.models import Driver
    from app.timeutil import now_utc_naive

    now = now_utc_naive()

    def visit(source, minutes_ago, *, ip="ip1", visitor="v1", tg=None):
        return SiteVisit(
            source=source, landing="/", device="mobile", visitor=visitor, ip_hash=ip, telegram_id=tg,
            created_at=now - timedelta(minutes=minutes_ago),
        )

    session.add_all([
        visit("yandex", 20, ip="ipA", visitor="vA"),
        visit("direct", 19, ip="ipA", visitor="vA2", tg=555),  # вход через бота с того же IP
        visit("google", 10, ip="ipB", visitor="vB"),
        visit("telegram", 5, ip="ipC", visitor="vC"),
    ])
    session.add(Driver(telegram_id=555, created_at=now - timedelta(minutes=19)))
    await session.commit()

    report = await reporting.traffic_overview(days=1)

    by_source = {row["source"]: row for row in report["rows"]}
    assert report["total"] == 3  # заход после входа — продолжение, не отдельный источник
    assert set(by_source) == {"yandex", "google", "telegram"}
    assert by_source["yandex"]["signups"] == 1
    assert report["new_users"] == 1 and report["signups_unknown"] == 0
