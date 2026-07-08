"""Surface requests that need a human: failed submits, failed receipts, and
submitted orders that have sat unfulfilled for days."""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..enums import RequestStatus
from ..models import DailyRequest, Store

# A submitted order untouched for this long probably fell through a crack.
STALE_SUBMITTED_DAYS = 3


def get_attention(db: Session, store_id: int | None = None) -> dict:
    """Returns {'items': [...], 'total': int}; each item has kind/label/request_id."""
    stmt = select(DailyRequest, Store).join(Store, Store.id == DailyRequest.store_id)
    if store_id:
        stmt = stmt.where(DailyRequest.store_id == store_id)
    stmt = stmt.where(
        DailyRequest.status.in_(
            [RequestStatus.SYNC_ERROR, RequestStatus.RECEIPT_ERROR, RequestStatus.SUBMITTED]
        )
    ).order_by(DailyRequest.id.desc())

    stale_cutoff = datetime.utcnow() - timedelta(days=STALE_SUBMITTED_DAYS)
    items: list[dict] = []
    for req, store in db.execute(stmt).all():
        if req.status == RequestStatus.SYNC_ERROR:
            items.append(
                {
                    "kind": "sync_error",
                    "request_id": req.id,
                    "label": f"{store.store_name} request #{req.id} ({req.request_date}) failed to submit to Unleashed",
                    "detail": (req.error_message or "")[:200],
                }
            )
        elif req.status == RequestStatus.RECEIPT_ERROR:
            items.append(
                {
                    "kind": "receipt_error",
                    "request_id": req.id,
                    "label": f"{store.store_name} request #{req.id} ({req.request_date}) completed in Unleashed but the receipt failed",
                    "detail": (req.error_message or "")[:200],
                }
            )
        elif req.submitted_at is not None and req.submitted_at < stale_cutoff:
            items.append(
                {
                    "kind": "stale",
                    "request_id": req.id,
                    "label": (
                        f"{store.store_name} request #{req.id} ({req.request_date}) has been submitted for "
                        f"{(datetime.utcnow() - req.submitted_at).days}+ days without completing"
                    ),
                    "detail": f"Unleashed order {req.unleashed_order_number or '—'}",
                }
            )
    return {"items": items, "total": len(items)}
