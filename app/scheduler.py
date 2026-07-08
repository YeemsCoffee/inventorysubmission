"""APScheduler setup: polling fallback + daily auto-submit.

The scheduler starts whenever Unleashed is configured. The polling job is
gated by POLLING_ENABLED; the auto-submit job is gated by the DB-backed
setting (Admin -> Settings) and is rescheduled live when the time changes.
"""
from __future__ import annotations

import logging

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from .config import get_settings
from .database import SessionLocal
from .services import settings_service
from .services.auto_submit_service import run_scheduled_auto_submit
from .services.sync_service import run_scheduled_poll

logger = logging.getLogger("scheduler")
_scheduler: BackgroundScheduler | None = None

AUTO_SUBMIT_JOB_ID = "auto_submit_daily"


def _sync_auto_submit_job(scheduler: BackgroundScheduler) -> None:
    """Add, update or remove the auto-submit cron job to match the DB config."""
    db = SessionLocal()
    try:
        cfg = settings_service.get_auto_submit_config(db)
    finally:
        db.close()

    existing = scheduler.get_job(AUTO_SUBMIT_JOB_ID)
    if not cfg["enabled"]:
        if existing is not None:
            scheduler.remove_job(AUTO_SUBMIT_JOB_ID)
            logger.info("Auto-submit job removed (disabled in settings)")
        return

    trigger = CronTrigger(
        hour=cfg["hour"], minute=cfg["minute"], timezone=pytz.timezone(cfg["timezone"])
    )
    scheduler.add_job(
        run_scheduled_auto_submit,
        trigger,
        id=AUTO_SUBMIT_JOB_ID,
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=3600,  # a restart near the fire time still runs it
    )
    logger.info(
        "Auto-submit scheduled daily at %s %s", cfg["time"], cfg["timezone"]
    )


def reschedule_auto_submit() -> None:
    """Apply a changed auto-submit config to the live scheduler (no restart)."""
    if _scheduler is not None:
        _sync_auto_submit_job(_scheduler)


def start_scheduler() -> None:
    global _scheduler
    settings = get_settings()
    if not settings.unleashed_configured:
        logger.info("Scheduler not started: Unleashed credentials not configured")
        return
    if _scheduler is not None:
        return
    _scheduler = BackgroundScheduler(daemon=True)

    if settings.polling_enabled:
        _scheduler.add_job(
            run_scheduled_poll,
            "interval",
            minutes=settings.polling_interval_minutes,
            id="poll_unleashed",
            max_instances=1,
            coalesce=True,
        )
        logger.info("Polling scheduled (every %s min)", settings.polling_interval_minutes)
    else:
        logger.info("Polling disabled (POLLING_ENABLED=false)")

    _sync_auto_submit_job(_scheduler)
    _scheduler.start()


def shutdown_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
