"""Общие мелочи форматирования сообщений Telegram."""


def driver_link(tg_user_id: int, label: str | None = None) -> str:
    """HTML-ссылка на диалог с водителем — открывается сразу тапом, без поиска по id."""
    label = label or str(tg_user_id)
    return f'<a href="tg://user?id={tg_user_id}">{label}</a>'
