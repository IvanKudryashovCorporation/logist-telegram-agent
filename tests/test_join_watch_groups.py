"""Распределение групп по трём аккаунтам для вступления: поровну по трафику, уже вступившему аккаунту — своя группа."""

from scripts.join_watch_groups import ACCOUNTS, assign


def _groups(n):
    return [{"id": i, "tg_chat_id": -1000 - i, "title": f"G{i}", "username": f"g{i}"} for i in range(1, n + 1)]


def test_groups_are_split_evenly_by_traffic():
    groups = _groups(9)
    weights = {g["id"]: 100 for g in groups}

    mapping = assign(groups, {}, weights, {name: 0 for name in ACCOUNTS})

    per_account = [list(mapping.values()).count(name) for name in ACCOUNTS]
    assert per_account == [3, 3, 3]


def test_heavy_group_does_not_overload_one_account():
    groups = _groups(4)
    weights = {1: 1000, 2: 10, 3: 10, 4: 10}

    mapping = assign(groups, {}, weights, {name: 0 for name in ACCOUNTS})

    heavy_owner = mapping["1"]
    assert [mapping[str(i)] for i in (2, 3, 4)].count(heavy_owner) == 0  # лёгкие группы уходят другим аккаунтам


def test_account_that_already_joined_keeps_the_group():
    groups = _groups(3)
    already = {"acc_4077": {-1002}}

    mapping = assign(groups, already, {1: 5, 2: 5, 3: 5}, {name: 0 for name in ACCOUNTS})

    assert mapping["2"] == "acc_4077"


def test_accounts_with_many_groups_already_get_fewer_new_ones():
    groups = _groups(6)
    base = {"logist": 3000, "acc_4077": 0, "acc_crimea": 0}  # у основного уже много групп

    mapping = assign(groups, {}, {g["id"]: 100 for g in groups}, base)

    assert list(mapping.values()).count("logist") < list(mapping.values()).count("acc_4077")


def test_rebalance_moves_pending_groups_away_from_the_main_account():
    from scripts.join_watch_groups import rebalance

    def group(account, status, weight):
        return {"account": account, "status": status, "weight": weight}

    state = {"groups": {
        "1": group("logist", "pending", 100), "2": group("logist", "pending", 50), "3": group("logist", "requested", 70),
        "4": group("logist", "joined", 10), "5": group("acc_4077", "pending", 60), "6": group("acc_crimea", "pending", 60),
    }}

    moved = rebalance(state)

    assert moved == 2
    assert {state["groups"]["1"]["account"], state["groups"]["2"]["account"]} <= {"acc_4077", "acc_crimea"}
    assert state["groups"]["3"]["account"] == "logist" and state["groups"]["4"]["account"] == "logist"  # заявка и вступление остаются
