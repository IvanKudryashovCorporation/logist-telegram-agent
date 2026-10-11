from app.models import WorkGroup
from scripts.prune_groups import select_doomed


def _row(chat_id, messages, spam_share=0.0):
    return {"chat_id": chat_id, "messages": messages, "spam_share": spam_share}


def test_select_doomed_picks_disabled_silent_and_single_spam_groups():
    groups = [
        WorkGroup(tg_chat_id=1, title="живая", is_active=True),
        WorkGroup(tg_chat_id=2, title="тишина", is_active=True),
        WorkGroup(tg_chat_id=3, title="один спам", is_active=True),
        WorkGroup(tg_chat_id=4, title="отключена", is_active=False),
        WorkGroup(tg_chat_id=5, title="одно сообщение, но не спам", is_active=True),
        WorkGroup(tg_chat_id=6, title="вообще нет в статистике", is_active=True),
    ]
    rows = [_row(1, 40, 0.5), _row(2, 0), _row(3, 1, 1.0), _row(5, 1, 0.0)]

    assert {g.tg_chat_id for g in select_doomed(groups, rows)} == {2, 3, 4, 6}
