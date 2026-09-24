"""Точка входа агента.

Слушает рабочие группы диспетчеров и разбирает заявки в БД — это агрегатор
заказов для водителей (см. app/web/), никакой переписки или публикации
агент больше не ведёт.
"""

import asyncio
import logging

from sqlalchemy import select

from app.config import settings
from app.db.base import SessionLocal
from app.models import WorkGroup
from app.telegram.client import build_client
from app.telegram.work_group import register_work_group_handlers

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s %(name)s: %(message)s",
)
log = logging.getLogger("agent")


async def main() -> None:
    client = build_client()
    await client.start()

    me = await client.get_me()
    log.info("Агент запущен от лица %s (id=%s)", me.first_name, me.id)

    async with SessionLocal() as session:
        # Разовый перенос группы из .env (старый способ настройки) в БД, если её
        # там ещё нет — дальше список групп живёт только в БД через
        # scripts.manage_work_groups, .env для этого больше не используется.
        if settings.work_group_chat_id:
            existing = (
                await session.execute(
                    select(WorkGroup).where(WorkGroup.tg_chat_id == settings.work_group_chat_id)
                )
            ).scalar_one_or_none()
            if existing is None:
                entity = await client.get_entity(settings.work_group_chat_id)
                session.add(
                    WorkGroup(
                        tg_chat_id=settings.work_group_chat_id,
                        title=getattr(entity, "title", str(settings.work_group_chat_id)),
                    )
                )
                await session.commit()
                log.info("Перенёс WORK_GROUP_CHAT_ID из .env в список рабочих групп.")

        work_group_ids = (
            await session.execute(select(WorkGroup.tg_chat_id).where(WorkGroup.is_active.is_(True)))
        ).scalars().all()

    if not work_group_ids:
        log.warning(
            "Нет ни одной активной рабочей группы — заявки читать неоткуда. "
            "Добавьте: python -m scripts.manage_work_groups add <chat_id_или_@username>"
        )
    else:
        register_work_group_handlers(client, list(work_group_ids))
        log.info("Слушаю заявки в %s рабочих группах.", len(work_group_ids))

    log.info("Ожидание событий. Ctrl+C для остановки.")
    await client.run_until_disconnected()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Остановлено пользователем.")
