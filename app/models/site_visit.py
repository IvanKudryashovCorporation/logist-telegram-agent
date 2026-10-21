"""Заход на сайт и то, откуда человек пришёл (см. app.web.traffic).

Одна строка — один «сеанс»: первая страница, на которую посетитель попал, и источник. Куки для этого нет:
посетитель опознаётся хэшем от IP и браузера, который каждый день меняется, поэтому по базе нельзя ни узнать
человека, ни проследить его за пределами суток.
"""

from typing import Optional

from sqlalchemy import BigInteger, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin


class SiteVisit(Base, TimestampMixin):
    __tablename__ = "site_visits"

    id: Mapped[int] = mapped_column(primary_key=True)
    #: Код источника: direct, yandex, google, telegram, vk, pwa, site (другой сайт), utm:<метка>...
    source: Mapped[str] = mapped_column(String(48), index=True, nullable=False)
    #: Домен, с которого перешли (без пути и параметров), если был Referer.
    referrer_host: Mapped[Optional[str]] = mapped_column(String(128))
    utm_source: Mapped[Optional[str]] = mapped_column(String(64))
    utm_medium: Mapped[Optional[str]] = mapped_column(String(64))
    utm_campaign: Mapped[Optional[str]] = mapped_column(String(64))
    #: Страница входа без параметров, например ``/about``.
    landing: Mapped[str] = mapped_column(String(96), nullable=False, default="/")
    #: mobile, desktop или telegram (встроенный браузер Telegram).
    device: Mapped[str] = mapped_column(String(16), nullable=False, default="desktop")
    #: Хэш «день + IP + браузер»: тот же человек в тот же день.
    visitor: Mapped[str] = mapped_column(String(16), index=True, nullable=False)
    #: Хэш «день + IP»: связывает заход в одном браузере с входом из другого (бот Telegram).
    ip_hash: Mapped[str] = mapped_column(String(16), index=True, nullable=False)
    #: Заполнен, если на момент захода человек был вошёл через Telegram.
    telegram_id: Mapped[Optional[int]] = mapped_column(BigInteger, index=True)

    def __repr__(self) -> str:
        return f"<SiteVisit {self.source} {self.landing}>"
