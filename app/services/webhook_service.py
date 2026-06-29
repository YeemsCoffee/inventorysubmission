"""Unleashed webhook intake.

Unleashed webhook deliveries carry a subscription id, an event notification id,
an event type, a timestamp and a small data payload (e.g. a Sales Order Guid) —
not the full record. So we store the event, dedupe on the notification id, then
re-fetch the order by Guid and run the same idempotent receipt path as polling.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..enums import WebhookStatus
from ..integrations.unleashed import UnleashedClient
from ..models import DailyRequest, WebhookEvent
from . import receipt_service

logger = logging.getLogger("webhook_service")


def _first(d: dict, *keys: str):
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
        # case-insensitive fallback
        for actual in d:
            if actual.lower() == k.lower() and d[actual] not in (None, ""):
                return d[actual]
    return None


def _extract(payload: dict) -> tuple[str | None, str | None, str | None]:
    """Return (event_notification_id, event_type, resource_guid) defensively."""
    event_id = _first(payload, "EventNotificationId", "NotificationId", "EventId", "Id")
    event_type = _first(payload, "EventType", "Type", "Event")
    data = payload.get("Data") or payload.get("data") or {}
    if isinstance(data, dict):
        guid = _first(data, "Guid", "SalesOrderGuid", "ResourceGuid", "Id")
    else:
        guid = None
    if not guid:
        guid = _first(payload, "Guid", "ResourceGuid", "SalesOrderGuid")
    return (str(event_id) if event_id else None, event_type, guid)


def handle_webhook(db: Session, payload: dict, client: UnleashedClient | None = None) -> dict:
    event_id, event_type, resource_guid = _extract(payload)

    event = WebhookEvent(
        provider="unleashed",
        event_type=event_type,
        event_notification_id=event_id,
        resource_guid=resource_guid,
        raw_payload=json.dumps(payload)[:10000],
        status=WebhookStatus.RECEIVED,
    )
    db.add(event)
    try:
        db.commit()
    except IntegrityError:
        # Duplicate delivery (same EventNotificationId) — already handled.
        db.rollback()
        logger.info("Duplicate webhook %s ignored", event_id)
        return {"status": "duplicate", "event_notification_id": event_id}

    if not resource_guid:
        event.status = WebhookStatus.IGNORED
        event.processed_at = datetime.utcnow()
        event.error_message = "no resource guid in payload"
        db.commit()
        return {"status": "ignored", "reason": "no resource guid"}

    req = db.execute(
        select(DailyRequest).where(DailyRequest.unleashed_sales_order_guid == resource_guid)
    ).scalars().first()
    if req is None:
        event.status = WebhookStatus.IGNORED
        event.processed_at = datetime.utcnow()
        event.error_message = "no matching local request"
        db.commit()
        return {"status": "ignored", "reason": "no matching request"}

    try:
        outcome = receipt_service.process_completion(
            db, request_id=req.id, client=client, source="webhook"
        )
        event.status = WebhookStatus.PROCESSED
        event.processed_at = datetime.utcnow()
        db.commit()
        return {"status": "processed", "request_id": req.id, "outcome": outcome.status}
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        event = db.get(WebhookEvent, event.id)
        event.status = WebhookStatus.ERROR
        event.error_message = str(exc)[:1000]
        event.processed_at = datetime.utcnow()
        db.commit()
        logger.exception("Webhook processing failed for event %s", event_id)
        return {"status": "error", "error": str(exc)}
