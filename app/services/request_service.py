"""Daily request generation + submission to Unleashed as a Sales Order.

Generation formula (per the spec):
    suggested = max(par_level - current_count, 0)
Only items needing stock (suggested > 0) are included. Managers may override the
final quantity before submission.

Submission is idempotent: the Sales Order Guid is generated and stored locally
*before* the API call, so a retry re-POSTs the SAME Guid — Unleashed updates that
order rather than creating a duplicate.
"""
from __future__ import annotations

import logging
import uuid
from datetime import date, datetime

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from ..config import get_settings
from ..enums import LineStatus, RequestStatus, TransactionType
from ..integrations.unleashed import UnleashedClient, UnleashedError
from ..models import (
    DailyRequest,
    DailyRequestLine,
    InventoryTransaction,
    Product,
    Store,
    StoreInventory,
)
from . import inventory_service

logger = logging.getLogger("request_service")
settings = get_settings()


class RequestError(Exception):
    pass


def generate_daily_request(
    db: Session, *, store_id: int, request_date: date | None = None
) -> DailyRequest:
    """Create (or refresh) a DRAFT request for a store from current count vs par.

    If an un-submitted draft already exists for the store+date it is refreshed in
    place, so clicking "Generate" twice never produces duplicate drafts.
    """
    request_date = request_date or date.today()
    store = db.get(Store, store_id)
    if store is None:
        raise RequestError(f"Unknown store {store_id}")

    existing = db.execute(
        select(DailyRequest).where(
            DailyRequest.store_id == store_id,
            DailyRequest.request_date == request_date,
            DailyRequest.status.in_([RequestStatus.DRAFT, RequestStatus.SYNC_ERROR]),
        )
    ).scalars().first()

    if existing is not None:
        req = existing
        # Ledger rows (e.g. SYNC_ERROR audits from a failed submit) may reference
        # the old lines. Detach them before deleting or the FK blocks the delete;
        # they stay linked to the request itself via daily_request_id.
        old_line_ids = [ln.id for ln in req.lines]
        if old_line_ids:
            db.execute(
                update(InventoryTransaction)
                .where(InventoryTransaction.daily_request_line_id.in_(old_line_ids))
                .values(daily_request_line_id=None)
            )
        for ln in list(req.lines):
            db.delete(ln)
        req.status = RequestStatus.DRAFT
        req.error_message = None
        req.generated_at = datetime.utcnow()
    else:
        req = DailyRequest(store_id=store_id, request_date=request_date, status=RequestStatus.DRAFT)
        db.add(req)
    db.flush()  # ensure req.id

    rows = db.execute(
        select(StoreInventory, Product)
        .join(Product, Product.id == StoreInventory.product_id)
        .where(StoreInventory.store_id == store_id, StoreInventory.active.is_(True))
        .order_by(Product.display_name)
    ).all()

    line_no = 0
    for inv, product in rows:
        suggested = inv.par_level - inv.current_count
        if suggested <= 0:
            continue
        line_no += 1
        line = DailyRequestLine(
            daily_request_id=req.id,
            product_id=product.id,
            sales_order_line_number=line_no,
            current_count_at_generation=inv.current_count,
            par_level=inv.par_level,
            suggested_quantity=suggested,
            final_requested_quantity=suggested,
            status=LineStatus.PENDING,
        )
        db.add(line)
        inventory_service.record_audit(
            db,
            store_id=store_id,
            product_id=product.id,
            transaction_type=TransactionType.DAILY_REQUEST_GENERATED,
            source="manager",
            daily_request_id=req.id,
            note=f"Suggested {suggested:g} (par {inv.par_level:g} - count {inv.current_count:g})",
        )

    db.commit()
    db.refresh(req)
    return req


def override_line(
    db: Session, *, line_id: int, final_quantity: float, note: str | None = None
) -> DailyRequestLine:
    line = db.get(DailyRequestLine, line_id)
    if line is None:
        raise RequestError(f"Unknown request line {line_id}")
    if line.request.status not in (RequestStatus.DRAFT, RequestStatus.SYNC_ERROR):
        raise RequestError("Cannot edit a request that has already been submitted")
    if final_quantity != line.final_requested_quantity:
        line.notes = note or line.notes
        inventory_service.record_audit(
            db,
            store_id=line.request.store_id,
            product_id=line.product_id,
            transaction_type=TransactionType.REQUEST_OVERRIDE,
            source="manager",
            daily_request_id=line.daily_request_id,
            daily_request_line_id=line.id,
            note=f"Override {line.final_requested_quantity:g} -> {final_quantity:g}. {note or ''}".strip(),
        )
        line.final_requested_quantity = final_quantity
    line.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(line)
    return line


def cancel_request(db: Session, *, request_id: int, cancelled_by: int | None = None) -> DailyRequest:
    """Retire a request locally (poller stops watching it).

    Does NOT touch Unleashed: if a Sales Order was created there, delete or
    cancel it in Unleashed as well.
    """
    req = db.get(DailyRequest, request_id)
    if req is None:
        raise RequestError(f"Unknown request {request_id}")
    if req.status == RequestStatus.CANCELLED:
        return req
    if req.status not in RequestStatus.CANCELLABLE:
        raise RequestError(f"Request {request_id} is {req.status} and can no longer be cancelled")

    req.status = RequestStatus.CANCELLED
    req.error_message = None
    for ln in req.lines:
        inventory_service.record_audit(
            db,
            store_id=req.store_id,
            product_id=ln.product_id,
            transaction_type=TransactionType.REQUEST_CANCELLED,
            source="manager",
            daily_request_id=req.id,
            daily_request_line_id=ln.id,
            unleashed_sales_order_guid=req.unleashed_sales_order_guid,
            unleashed_order_number=req.unleashed_order_number,
            note="Request cancelled locally",
        )
    db.commit()
    db.refresh(req)
    logger.info("Request %s cancelled by user %s", req.id, cancelled_by)
    return req


def _build_payload(req: DailyRequest, store: Store, lines: list[tuple[DailyRequestLine, Product]]) -> dict:
    so_lines = []
    for line, product in lines:
        product_ref: dict = {"ProductCode": product.product_code}
        if product.unleashed_product_guid:
            product_ref["Guid"] = product.unleashed_product_guid
        so_lines.append(
            {
                "LineNumber": line.sales_order_line_number,
                "Product": product_ref,
                "OrderQuantity": line.final_requested_quantity,
            }
        )

    customer: dict = {"CustomerCode": store.unleashed_customer_code}
    if store.unleashed_customer_guid:
        customer["Guid"] = store.unleashed_customer_guid

    payload: dict = {
        "Guid": req.unleashed_sales_order_guid,
        "OrderStatus": settings.unleashed_create_order_status,  # API allows Parked/Completed only
        "Customer": customer,
        # Stores are not warehouses: the order still ships FROM a real warehouse.
        "Warehouse": {"WarehouseCode": settings.unleashed_fulfill_warehouse_code},
        "SourceId": req.unleashed_source_id,  # external ref back to this DailyRequest
        "SalesOrderLines": so_lines,
        "Comments": f"Auto replenishment for {store.store_name} ({req.request_date})",
    }
    if settings.unleashed_default_currency:
        payload["Currency"] = {"CurrencyCode": settings.unleashed_default_currency}
    if settings.unleashed_default_tax_code:
        payload["Tax"] = {"TaxCode": settings.unleashed_default_tax_code}
    return payload


def submit_to_unleashed(
    db: Session, *, request_id: int, submitted_by: int | None = None, client: UnleashedClient | None = None
) -> DailyRequest:
    req = db.get(DailyRequest, request_id)
    if req is None:
        raise RequestError(f"Unknown request {request_id}")
    if req.status not in (RequestStatus.DRAFT, RequestStatus.SYNC_ERROR):
        raise RequestError(f"Request {request_id} is {req.status}, not submittable")

    store = db.get(Store, req.store_id)
    lines = [
        (ln, db.get(Product, ln.product_id))
        for ln in req.lines
        if ln.final_requested_quantity and ln.final_requested_quantity > 0
    ]
    if not lines:
        raise RequestError("No lines with a positive quantity to submit")

    # Generate idempotency anchors BEFORE the API call and persist them, so a
    # retry reuses the same Guid (Unleashed upserts on Guid -> no duplicate order).
    if not req.unleashed_sales_order_guid:
        req.unleashed_sales_order_guid = str(uuid.uuid4())
    if not req.unleashed_source_id:
        req.unleashed_source_id = f"CAFEAPP-DR-{req.id}"
    # Ensure stable line numbers for later shipment matching.
    for idx, (ln, _product) in enumerate(lines, start=1):
        if not ln.sales_order_line_number:
            ln.sales_order_line_number = idx
    db.commit()

    client = client or UnleashedClient()
    payload = _build_payload(req, store, lines)

    try:
        resp = client.create_sales_order(req.unleashed_sales_order_guid, payload)
    except UnleashedError as exc:
        req.status = RequestStatus.SYNC_ERROR
        req.error_message = str(exc)[:1000]
        for ln, product in lines:
            inventory_service.record_audit(
                db,
                store_id=req.store_id,
                product_id=product.id,
                transaction_type=TransactionType.SYNC_ERROR,
                source="manager",
                daily_request_id=req.id,
                daily_request_line_id=ln.id,
                note="Sales Order submission failed",
            )
        db.commit()
        logger.warning("Sales order submit failed for request %s: %s", req.id, exc)
        raise

    # Success: capture the Unleashed-assigned order number (if returned).
    order_number = resp.get("OrderNumber") if isinstance(resp, dict) else None
    if order_number:
        req.unleashed_order_number = order_number
    if isinstance(resp, dict) and resp.get("Guid"):
        req.unleashed_sales_order_guid = resp["Guid"]
    req.status = RequestStatus.SUBMITTED
    req.submitted_at = datetime.utcnow()
    req.submitted_by = submitted_by
    req.error_message = None
    for ln, product in lines:
        ln.status = LineStatus.SUBMITTED
        inventory_service.record_audit(
            db,
            store_id=req.store_id,
            product_id=product.id,
            transaction_type=TransactionType.UNLEASHED_REQUEST_SUBMITTED,
            source="manager",
            daily_request_id=req.id,
            daily_request_line_id=ln.id,
            unleashed_sales_order_guid=req.unleashed_sales_order_guid,
            unleashed_order_number=req.unleashed_order_number,
            note=f"Submitted qty {ln.final_requested_quantity:g}",
        )
    db.commit()
    db.refresh(req)
    logger.info("Submitted request %s as Unleashed order %s", req.id, req.unleashed_order_number)
    return req
