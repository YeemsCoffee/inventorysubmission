"""Scheduled end-of-day auto-submission of replenishment requests.

At the configured time (settings table, editable in Admin -> Settings) every
active store's daily request is submitted to Unleashed:

- no request yet today            -> generate from current counts, then submit
- untouched draft exists          -> regenerate (fresh end-of-day counts), submit
- manager-curated draft exists    -> submit AS-IS (overrides are respected)
- already submitted / received    -> skip
- cancelled today                 -> skip (a manager said "not today")
- nothing below par               -> skip (no empty orders)

Failures mark the request SYNC_ERROR exactly like a manual submit; the next
day's run (or a manual retry) reuses the same order Guid, so no duplicates.
"""
from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..database import SessionLocal
from ..enums import RequestStatus
from ..integrations.unleashed import UnleashedClient, UnleashedError
from ..models import DailyRequest, Store
from . import request_service, settings_service

logger = logging.getLogger("auto_submit")

_SKIP_STATUSES = {
    RequestStatus.SUBMITTED,
    RequestStatus.COMPLETED,
    RequestStatus.RECEIVED,
    RequestStatus.RECEIPT_ERROR,
    RequestStatus.CANCELLED,
}


def run_auto_submit(db: Session | None = None, client: UnleashedClient | None = None) -> dict:
    """Submit today's request for every active store. Returns a summary dict."""
    own_session = db is None
    db = db or SessionLocal()
    summary = {"submitted": 0, "skipped": 0, "empty": 0, "errors": 0}
    try:
        cfg = settings_service.get_auto_submit_config(db)
        if not cfg["enabled"]:
            logger.info("Auto-submit is disabled; nothing to do")
            return summary

        client = client or UnleashedClient()
        today = settings_service.local_today(db)
        stores = list(db.execute(select(Store).where(Store.active.is_(True))).scalars())

        for store in stores:
            req = db.execute(
                select(DailyRequest)
                .where(DailyRequest.store_id == store.id, DailyRequest.request_date == today)
                .order_by(DailyRequest.id.desc())
            ).scalars().first()

            if req is not None and req.status in _SKIP_STATUSES:
                summary["skipped"] += 1
                continue

            # Respect manager-curated drafts; refresh untouched ones so the
            # order reflects end-of-day counts.
            curated = req is not None and any(
                ln.final_requested_quantity != ln.suggested_quantity for ln in req.lines
            )
            if not curated:
                req = request_service.generate_daily_request(db, store_id=store.id, request_date=today)

            if not any(ln.final_requested_quantity > 0 for ln in req.lines):
                summary["empty"] += 1
                continue

            try:
                request_service.submit_to_unleashed(db, request_id=req.id, client=client)
                summary["submitted"] += 1
            except (UnleashedError, request_service.RequestError) as exc:
                # Request is already marked SYNC_ERROR with the message stored.
                logger.warning("Auto-submit failed for %s (request %s): %s", store.store_code, req.id, exc)
                summary["errors"] += 1
        return summary
    finally:
        if own_session:
            db.close()


def run_scheduled_auto_submit() -> None:
    """APScheduler entry point. Never raises — the scheduler must keep running."""
    try:
        summary = run_auto_submit()
        logger.info("Auto-submit sweep: %s", summary)
    except Exception:  # noqa: BLE001
        logger.exception("Auto-submit sweep crashed")
