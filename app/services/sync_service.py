"""Polling fallback — detect Completed Sales Orders without webhooks.

Runs on a schedule (and can be triggered manually by an admin). For every local
request that is submitted-but-not-yet-received it re-reads the Unleashed order;
if Completed it runs the same idempotent receipt path used by the webhook.
"""
from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..database import SessionLocal
from ..enums import RequestStatus
from ..integrations.unleashed import UnleashedClient
from ..models import DailyRequest
from . import receipt_service

logger = logging.getLogger("sync_service")


def poll_open_requests(db: Session | None = None, client: UnleashedClient | None = None) -> dict:
    """Process all open requests once. Returns a small summary dict."""
    own_session = db is None
    db = db or SessionLocal()
    client = client or UnleashedClient()
    summary = {"checked": 0, "received": 0, "errors": 0}
    try:
        open_requests = db.execute(
            select(DailyRequest).where(
                DailyRequest.status.in_(list(RequestStatus.OPEN_FOR_RECEIPT)),
                DailyRequest.unleashed_sales_order_guid.is_not(None),
            )
        ).scalars().all()

        for req in open_requests:
            summary["checked"] += 1
            try:
                outcome = receipt_service.process_completion(
                    db, request_id=req.id, client=client, source="poll"
                )
                if outcome.status == RequestStatus.RECEIVED:
                    summary["received"] += 1
                elif outcome.status == RequestStatus.RECEIPT_ERROR:
                    summary["errors"] += 1
            except Exception:  # noqa: BLE001 - never let one bad order stop the sweep
                logger.exception("Polling failed for request %s", req.id)
                summary["errors"] += 1
        return summary
    finally:
        if own_session:
            db.close()


def run_scheduled_poll() -> None:
    """Entry point for APScheduler. Swallows errors so the scheduler keeps running."""
    try:
        summary = poll_open_requests()
        if summary["checked"]:
            logger.info("Polling sweep: %s", summary)
    except Exception:  # noqa: BLE001
        logger.exception("Scheduled polling sweep crashed")
