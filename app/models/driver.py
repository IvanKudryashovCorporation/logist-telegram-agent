"""Водитель, вошедший через Telegram Login Widget.

До этой модели личность водителя — это анонимная cookie (см. app.web.deps.
driver_token). Она никуда не делась: у водителя БЕЗ входа продолжает жить
``driver_id`` с рандомным токеном, но такой водитель не может открыть
карточку заказа и "Мои заказы" (см. app.web.auth) — только листать ленту.

После входа через Telegram driver_id-токен становится СТАБИЛЬНЫМ:
``f"tg:{telegram_id}"`` вместо случайного UUID (см. app.web.auth.driver_token_for).
Это осознанный выбор — весь остальной код (Order.taken_by_token, /my, /orders/*)
продолжает работать без изменений, просто токен теперь переживает смену
браузера/устройства, а не только для этого браузера.
"""

from datetime import date, datetime
from typing import Optional

from sqlalchemy import BigInteger, Boolean, Date, DateTime, Integer, String, false
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin


class Driver(Base, TimestampMixin):
    __tablename__ = "drivers"

    id: Mapped[int] = mapped_column(primary_key=True)

    #: id аккаунта в Telegram — стабильный, не меняется. Уникален: один
    #: Telegram-аккаунт не может завести две записи водителя.
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True, nullable=False)
    username: Mapped[Optional[str]] = mapped_column(String(64))
    first_name: Mapped[Optional[str]] = mapped_column(String(128))
    last_name: Mapped[Optional[str]] = mapped_column(String(128))
    photo_url: Mapped[Optional[str]] = mapped_column(String(512))

    #: Обновляется при каждом входе — не то же самое, что created_at
    #: (TimestampMixin), которая фиксирует именно первую регистрацию.
    last_login_at: Mapped[Optional[datetime]] = mapped_column(DateTime())

    # --- Подписка (см. app/services/billing.py) ---
    #: Конец пробного периода. Новым водителям ставится при регистрации; пусто у тех, кто зарегистрирован до запуска
    #: подписки: у них пробный период считается от ``SUBSCRIPTION_LAUNCH_DATE``.
    trial_ends_at: Mapped[Optional[datetime]] = mapped_column(DateTime())
    #: До какого момента оплачено (наивный UTC). Оплаты копятся поверх остатка пробного периода.
    paid_until: Mapped[Optional[datetime]] = mapped_column(DateTime())
    #: Продлевать картой автоматически и способ оплаты, сохранённый у провайдера.
    auto_renew: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false(), nullable=False)
    payment_method_id: Mapped[Optional[str]] = mapped_column(String(64))
    #: Какая карта сохранена: последние 4 цифры и тип (Visa, Mir...) — только для показа водителю.
    card_last4: Mapped[Optional[str]] = mapped_column(String(4))
    card_brand: Mapped[Optional[str]] = mapped_column(String(24))
    #: Подряд неудачных автосписаний; после трёх автопродление выключается.
    renew_failures: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    #: Когда водитель последний раз открывал колокольчик: уведомления новее — непрочитанные.
    notifications_seen_at: Mapped[Optional[datetime]] = mapped_column(DateTime())
    #: Для какой даты окончания уже отправлено напоминание (чтобы не слать его каждые полминуты).
    reminded_for: Mapped[Optional[date]] = mapped_column(Date())

    @property
    def token(self) -> str:
        """Значение, которое идёт в Order.taken_by_token и cookie driver_id."""
        return f"tg:{self.telegram_id}"

    @property
    def display_name(self) -> str:
        if self.username:
            return f"@{self.username}"
        return " ".join(part for part in (self.first_name, self.last_name) if part) or f"id{self.telegram_id}"

    def __repr__(self) -> str:
        return f"<Driver tg:{self.telegram_id} {self.username or self.first_name}>"
