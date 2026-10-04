"""Фоновые сервисы агента: уведомления владельца и обслуживание данных."""

from app.services.cleanup import (
    advance_agreed_orders,
    expire_stale_orders,
    prune_old_notifications,
    prune_old_stats,
    purge_old_orders,
    run_cleanup_loop,
)
from app.services.dedupe import cancel_duplicate_orders
from app.services.geocode import geocode_pending, run_geocode_worker
from app.services.notify import Notifier, notifier
from app.services.routes import heal_stale_routes, route_pending, run_routing_worker
from app.services.subscriptions import notify_once, run_subscription_worker

__all__ = [
    "Notifier",
    "advance_agreed_orders",
    "cancel_duplicate_orders",
    "expire_stale_orders",
    "geocode_pending",
    "heal_stale_routes",
    "notifier",
    "notify_once",
    "prune_old_notifications",
    "prune_old_stats",
    "purge_old_orders",
    "route_pending",
    "run_cleanup_loop",
    "run_geocode_worker",
    "run_routing_worker",
    "run_subscription_worker",
]
