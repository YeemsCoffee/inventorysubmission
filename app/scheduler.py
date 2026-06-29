"""APScheduler setup for the polling fallback.

Disabled automatically when polling is off or Unleashed is not configured (e.g.
local dev without credentials), so the app still boots cleanly.
"""
from __future__ import annotations

import logging

from apscheduler.schedulers.background import BackgroundScheduler

from .config import get_settings
from .services.sync_service import run_scheduled_poll

logger = logging.getLogger("scheduler")
_scheduler: BackgroundScheduler | None = None


def start_scheduler() -> None:
    global _scheduler
    settings = get_settings()
    if not settings.polling_enabled:
        logger.info("Polling disabled (POLLING_ENABLED=false)")
        return
    if not settings.unleashed_configured:
        logger.info("Polling not started: Unleashed credentials not configured")
        return
    if _scheduler is not None:
        return
    _scheduler = BackgroundScheduler(daemon=True)
    _scheduler.add_job(
        run_scheduled_poll,
        "interval",
        minutes=settings.polling_interval_minutes,
        id="poll_unleashed",
        max_instances=1,
        coalesce=True,
    )
    _scheduler.start()
    logger.info("Polling scheduler started (every %s min)", settings.polling_interval_minutes)


def shutdown_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
