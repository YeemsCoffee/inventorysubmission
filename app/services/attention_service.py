"""Surface requests that need a human: failed submits, failed receipts, and
submitted orders that have sat unfulfilled for days.

Rendered globally via base.html for every logged-in manager/warehouse/admin
(see templating.render), so the query stays bounded: all filtering happens in
SQL against the indexed status column, capped at MAX_ITEMS + 1.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from ..enums import RequestStatus
from ..models import DailyRequest, Store

# A submitted order untouched for this long probably fell through a crack.
STALE_SUBMITTED_DAYS = 3
# Cap what one banner shows; anything beyond renders as "and more".
MAX_ITEMS = 20


def get_attention(db: Session, store_id: int | None = None) -> dict:
    """Returns {'items': [...], 'total': int, 'truncated': bool}."""
    stale_cutoff = datetime.utcnow() - timedelta(days=STALE_SUBMITTED_DAYS)
    stmt = (
        select(DailyRequest, Store)
        .join(Store, Store.id == DailyRequest.store_id)
        .where(
            or_(
                DailyRequest.status.in_([RequestStatus.SYNC_ERROR, RequestStatus.RECEIPT_ERROR]),
                and_(
                    DailyRequest.status == RequestStatus.SUBMITTED,
                    DailyRequest.submitted_at.is_not(None),
                    DailyRequest.submitted_at < stale_cutoff,
                ),
            )
        )
        .order_by(DailyRequest.id.desc())
        .limit(MAX_ITEMS + 1)
    )
    if store_id:
        stmt = stmt.where(DailyRequest.store_id == store_id)

    rows = db.execute(stmt).all()
    truncated = len(rows) > MAX_ITEMS
    items: list[dict] = []
    for req, store in rows[:MAX_ITEMS]:
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
        else:
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
    return {"items": items, "total": len(items), "truncated": truncated}
