"""Ручной прогон обслуживания данных: протухание заявок и чистка метрик.

    python -m scripts.cleanup_orders --dry-run   # только показать, что будет
    python -m scripts.cleanup_orders             # выполнить
    python -m scripts.cleanup_orders --grace-hours 0

В норме это делает фоновый цикл внутри ``main.py`` (:func:`app.services.run_cleanup_loop`
раз в ``EXPIRE_POLL_MINUTES``). Скрипт нужен, чтобы:

* проверить эффект до включения фона (``--dry-run`` ничего не меняет);
* навести порядок после долгого простоя агента, когда в ленте скопились
  заявки с давно прошедшим временем подачи;
* разово почистить ``parse_stats`` после смены ``STATS_RETENTION_DAYS``.

Заявки, уже взятые водителем (``taken_by_token IS NOT NULL``), не трогаются —
это личная история водителя, а не лента.
"""

import argparse
import asyncio
from datetime import timedelta

from sqlalchemy import func, select

from app.config import settings
from app.db.base import SessionLocal
from app.models import OPEN_STATUSES, Order, OrderStatus, ParseStat
from app.services.cleanup import expire_stale_orders, prune_old_stats, purge_old_orders
from app.timeutil import now_msk_naive, now_utc_naive


async def _preview(grace_hours: int, retention_days: int) -> None:
    """Сколько строк затронет прогон — без единого UPDATE/DELETE."""
    cutoff = now_msk_naive() - timedelta(hours=max(0, grace_hours))
    async with SessionLocal() as session:
        stale = (
            await session.execute(
                select(func.count(Order.id)).where(
                    Order.status.in_(OPEN_STATUSES),
                    Order.pickup_at.is_not(None),
                    Order.pickup_at < cutoff,
                    Order.taken_by_token.is_(None),
                )
            )
        ).scalar_one()
        total_open = (
            await session.execute(
                select(func.count(Order.id)).where(Order.status.in_(OPEN_STATUSES))
            )
        ).scalar_one()
        orders_cutoff = now_utc_naive() - timedelta(days=settings.closed_orders_retention_days)
        to_purge = 0
        if settings.closed_orders_retention_days > 0:
            to_purge = (
                await session.execute(
                    select(func.count(Order.id)).where(
                        Order.status.in_((OrderStatus.EXPIRED, OrderStatus.CANCELLED)),
                        Order.taken_by_token.is_(None),
                        Order.updated_at < orders_cutoff,
                    )
                )
            ).scalar_one()
        old_stats = 0
        if retention_days > 0:
            stats_cutoff = now_utc_naive() - timedelta(days=retention_days)
            old_stats = (
                await session.execute(
                    select(func.count(ParseStat.id)).where(ParseStat.created_at < stats_cutoff)
                )
            ).scalar_one()

    print(f"Открытых заказов сейчас:      {total_open}")
    print(f"Станут EXPIRED:               {stale}  (подача раньше {cutoff:%d.%m.%Y %H:%M} МСК)")
    print(
        f"Удалится закрытых заказов:    {to_purge}  "
        f"(без владельца, старше {settings.closed_orders_retention_days} дн.)"
    )
    print(f"Удалится строк parse_stats:   {old_stats}  (старше {retention_days} дн.)")
    print("\nСухой режим — база не изменена. Для выполнения уберите --dry-run.")


async def main(grace_hours: int, retention_days: int, dry_run: bool) -> None:
    if dry_run:
        await _preview(grace_hours, retention_days)
        return

    # Не cleanup_once(): он берёт пороги из settings, а здесь их мог переопределить
    # вызывающий через CLI — иначе флаг --grace-hours работал бы только в dry-run.
    expired = await expire_stale_orders(grace_hours=grace_hours)
    purged = await purge_old_orders()
    pruned = await prune_old_stats(retention_days=retention_days)
    print(f"Переведено в EXPIRED:      {expired}")
    print(f"Удалено закрытых заказов:  {purged}")
    print(f"Удалено строк parse_stats: {pruned}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="только показать эффект")
    parser.add_argument(
        "--grace-hours", type=int, default=settings.expire_grace_hours,
        help=f"сколько часов после подачи держать заявку в ленте (по умолчанию {settings.expire_grace_hours})",
    )
    parser.add_argument(
        "--retention-days", type=int, default=settings.stats_retention_days,
        help=f"сколько дней хранить parse_stats (0 — не удалять; по умолчанию {settings.stats_retention_days})",
    )
    args = parser.parse_args()
    asyncio.run(main(args.grace_hours, args.retention_days, args.dry_run))
