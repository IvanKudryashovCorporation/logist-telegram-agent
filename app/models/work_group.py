"""Рабочие группы диспетчеров, откуда агент разбирает заявки (Этап 1).

Раньше поддерживалась только одна группа через WORK_GROUP_CHAT_ID в .env;
теперь список групп ведётся в БД (как и DriverGroup) — можно добавлять
сколько угодно без перезапуска с новым .env, только через scripts.manage_work_groups.
"""

from typing import Optional

from sqlalchemy import BigInteger, Boolean, String, false
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin


class WorkGroup(Base, TimestampMixin):
    __tablename__ = "work_groups"

    id: Mapped[int] = mapped_column(primary_key=True)

    tg_chat_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(256), nullable=False)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    #: Каким Telegram-аккаунтом читается группа: имя файла сессии рядом с
    #: проектом (``acc_crimea`` -> ``acc_crimea.session``). NULL — основная
    #: сессия агента (TG_SESSION_NAME).
    session_name: Mapped[Optional[str]] = mapped_column(String(64))
    #: «Наблюдение без вступления»: аккаунт в группе не состоит, а читает её публичную
    #: историю опросом (app.telegram.watcher). События Telegram для такой группы не приходят.
    watch_only: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false(), nullable=False)
    #: Публичный username группы (без @): по нему она находится без вступления.
    username: Mapped[Optional[str]] = mapped_column(String(64))
    #: До какого сообщения уже прочитано (только для ``watch_only``).
    last_message_id: Mapped[Optional[int]] = mapped_column(BigInteger)

    def __repr__(self) -> str:
        return f"<WorkGroup {self.title}>"
