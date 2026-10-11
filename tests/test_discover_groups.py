"""Поиск новых групп: что считать заявкой и как оценивать поток (без обращения к Telegram)."""

import json
from datetime import datetime, timedelta, timezone

from scripts import discover_groups as d

NOW = datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)


def test_order_like_messages_are_recognised():
    assert d.looks_like_order("Краснодар - Анапа 18:00, 3 пассажира, 4000 руб")
    assert d.looks_like_order("Сейчас Курск - Тольятти 30000 водителю")
    assert d.looks_like_order("Откуда: Керчь Куда: Краснодар 2 человека")


def test_job_ads_and_one_city_messages_are_not_orders():
    # спам с подработками и объявления без маршрута: в них есть цена, но заявками они не являются
    assert not d.looks_like_order("Маляр на внутреннюю отделку — 5300 за день. С 08:00 до 17:00, пишите в лс")
    assert not d.looks_like_order("Шабашка на 3–4 часа. Оплата 5500–6500 ₽ сразу. Нужны люди, пишите в лс")
    assert not d.looks_like_order("Свободен — Ворошиловский район Донецк +79496994287, можно ехать сейчас")
    assert not d.looks_like_order("Добро пожаловать в NEW TRANSFER! Комфортные автомобили, опытные водители, Москва")


def test_distinct_cities_counts_aliases_once():
    assert d.distinct_cities("Краснодар - Анапа") == 2
    assert d.distinct_cities("Краснодар Краснодар Краснодар") == 1


def test_chatter_and_driver_offers_are_not_orders():
    assert not d.looks_like_order("ок")
    assert not d.looks_like_order("Спасибо, принял, до связи!")
    assert not d.looks_like_order("Еду Горловка-Ростов в воскресенье. Есть места без пешего перехода.")
    assert not d.looks_like_order(None)


def test_rate_is_exact_when_the_sample_covers_a_day():
    texts = ["Краснодар - Анапа 18:00 4000 руб"] * 10 + ["ок"] * 5
    dates = [NOW - timedelta(hours=h) for h in (1, 2, 3, 4, 5, 6, 7, 8, 9, 30)] + [NOW - timedelta(hours=40)] * 5

    per_day, in_sample = d.estimate_orders_per_day(dates, texts, NOW)

    assert in_sample == 10 and per_day == 9.0  # девять заявок за последние 24 часа (десятая — 30 часов назад)


def test_rate_is_extrapolated_when_the_sample_is_shorter_than_a_day():
    texts = ["Краснодар - Анапа 18:00 4000 руб"] * 20
    dates = [NOW - timedelta(minutes=m * 12) for m in range(20)]  # 20 заявок за ~3,8 часа

    per_day, _ = d.estimate_orders_per_day(dates, texts, NOW)

    assert 100 <= per_day <= 140  # около 126 в сутки


def test_empty_sample_gives_zero():
    assert d.estimate_orders_per_day([], [], NOW) == (0.0, 0)


def test_relevant_titles():
    assert d.relevant("Такси межгород Крым")
    assert d.relevant("Transfer RF")
    assert not d.relevant("Рецепты и кулинария")


def test_ranking_puts_busiest_readable_first_and_drops_unreadable():
    a = d.Candidate(id=1, username="a", title="A", kind="группа", readable=True, sample=100, orders_per_day=30)
    b = d.Candidate(id=2, username="b", title="B", kind="группа", readable=True, sample=100, orders_per_day=80)
    c = d.Candidate(id=3, username="c", title="C", kind="группа", readable=False, error="ChannelPrivateError")
    e = d.Candidate(id=4, username="e", title="E", kind="канал", readable=True, sample=0)

    assert [x.username for x in d.rank([a, b, c, e])] == ["b", "a"]


def test_known_groups_file_is_loaded_in_both_forms(tmp_path):
    path = tmp_path / "known.json"
    path.write_text(json.dumps({"ids": [-1001, -1002], "usernames": ["@Taxi_One", "other", None]}), encoding="utf-8")

    ids, names = d.load_known(path)

    assert ids == {-1001, -1002} and names == {"taxi_one", "other"}
    assert d.load_known(None) == (set(), set()) and d.load_known(tmp_path / "missing.json") == (set(), set())


def test_default_queries_cover_cities():
    queries = d.all_queries()
    assert "такси межгород" in queries and "такси межгород Сочи" in queries
    assert d.all_queries(["свой запрос"]) == ["свой запрос"]
