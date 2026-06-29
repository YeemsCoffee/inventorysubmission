"""Receipt processing — turn a Completed Unleashed order into local stock.

Completion is only the *trigger*. We never blindly add requested quantities.
Instead we read what was actually shipped/fulfilled and add that:

  Preferred : Sales Shipments -> ShipmentQty per line (actual shipped).
  Fallback  : Completed Sales Order line OrderQuantity (only if the business
              guarantees Completed == fully delivered; configurable).

Idempotency:
  * Each receipt unit carries a unique idempotency_key (shipment line or order
    line), so inventory is never double-added across duplicate webhooks/polls.
  * Line/request roll-ups are RECOMPUTED from the ledger (sum of applied receipt
    transactions), so a crash mid-process self-heals on retry.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..enums import (
    UNLEASHED_COMPLETED_STATUS,
    LineStatus,
    RequestStatus,
    TransactionType,
)
from ..integrations.unleashed import UnleashedClient, UnleashedError
from ..models import DailyRequest, DailyRequestLine, Product, StoreInventory
from . import inventory_service

logger = logging.getLogger("receipt_service")
settings = get_settings()


@dataclass
class ReceiptLine:
    product_code: str
    quantity: float
    sales_order_line_number: int | None
    idempotency_key: str
    shipment_guid: str | None = None


@dataclass
class ReceiptOutcome:
    status: str
    applied: int = 0
    skipped: int = 0
    messages: list[str] = field(default_factory=list)


def _order_status(order: dict) -> str | None:
    return order.get("OrderStatus") if isinstance(order, dict) else None


def _collect_shipment_lines(shipments: list[dict]) -> list[ReceiptLine]:
    """Flatten Sales Shipments into receipt lines using actual ShipmentQty."""
    out: list[ReceiptLine] = []
    for shp in shipments:
        shp_guid = shp.get("Guid")
        for ln in shp.get("ShipmentLines", []) or []:
            product = ln.get("Product") or {}
            code = product.get("ProductCode") or ln.get("ProductCode")
            qty = ln.get("ShipmentQty") or 0
            so_line = ln.get("SalesOrderLineNumber")
            if not code or not qty:
                continue
            # Prefer the shipment line's own Guid for the key; fall back to a
            # composite that is still unique per shipped line.
            line_guid = ln.get("Guid")
            if line_guid:
                key = f"recv:shipline:{line_guid}"
            else:
                key = f"recv:ship:{shp_guid}:line:{so_line}:prod:{code}"
            out.append(
                ReceiptLine(
                    product_code=code,
                    quantity=float(qty),
                    sales_order_line_number=so_line,
                    idempotency_key=key,
                    shipment_guid=shp_guid,
                )
            )
    return out


def _collect_order_lines(order: dict) -> list[ReceiptLine]:
    """Fallback: use completed Sales Order line quantities as delivered."""
    out: list[ReceiptLine] = []
    so_guid = order.get("Guid")
    for ln in order.get("SalesOrderLines", []) or []:
        product = ln.get("Product") or {}
        code = product.get("ProductCode")
        qty = ln.get("OrderQuantity") or 0
        line_no = ln.get("LineNumber")
        if not code or not qty:
            continue
        out.append(
            ReceiptLine(
                product_code=code,
                quantity=float(qty),
                sales_order_line_number=line_no,
                idempotency_key=f"recv:order:{so_guid}:line:{line_no}:prod:{code}",
            )
        )
    return out


def _recompute_rollups(db: Session, req: DailyRequest) -> None:
    """Recompute each line's received total from the ledger (idempotent)."""
    sums = dict(
        db.execute(
            select(
                inventory_service.InventoryTransaction.daily_request_line_id,
                func.sum(inventory_service.InventoryTransaction.quantity_delta),
            )
            .where(
                inventory_service.InventoryTransaction.daily_request_id == req.id,
                inventory_service.InventoryTransaction.transaction_type
                == TransactionType.UNLEASHED_RECEIPT,
            )
            .group_by(inventory_service.InventoryTransaction.daily_request_line_id)
        ).all()
    )
    all_received = True
    any_received = False
    for line in req.lines:
        received = float(sums.get(line.id, 0) or 0)
        line.fulfilled_quantity = received
        line.received_into_store_count = received
        if received > 0:
            any_received = True
        if received >= (line.final_requested_quantity or 0) and line.final_requested_quantity > 0:
            line.status = LineStatus.RECEIVED
        else:
            if line.final_requested_quantity and line.final_requested_quantity > 0:
                all_received = False
    if all_received and any_received:
        req.status = RequestStatus.RECEIVED
        req.received_at = req.received_at or datetime.utcnow()
    else:
        req.status = RequestStatus.COMPLETED


def process_completion(
    db: Session,
    *,
    request_id: int,
    client: UnleashedClient | None = None,
    source: str = "poll",
) -> ReceiptOutcome:
    """Detect completion and apply actual fulfilled quantities. Idempotent."""
    req = db.get(DailyRequest, request_id)
    if req is None:
        return ReceiptOutcome(status="unknown", messages=["request not found"])
    if req.status == RequestStatus.RECEIVED:
        return ReceiptOutcome(status=RequestStatus.RECEIVED, messages=["already received"])
    if not req.unleashed_sales_order_guid:
        return ReceiptOutcome(status=req.status, messages=["not submitted to Unleashed"])

    client = client or UnleashedClient()

    try:
        order = client.get_sales_order(req.unleashed_sales_order_guid)
    except UnleashedError as exc:
        logger.warning("Receipt: failed to fetch order for request %s: %s", req.id, exc)
        return ReceiptOutcome(status=req.status, messages=[f"fetch failed: {exc}"])

    if _order_status(order) != UNLEASHED_COMPLETED_STATUS:
        # Not done yet — leave as submitted, nothing to receive.
        return ReceiptOutcome(status=req.status, messages=["order not completed yet"])

    req.completed_at = req.completed_at or datetime.utcnow()
    if req.status == RequestStatus.SUBMITTED:
        req.status = RequestStatus.COMPLETED
    db.commit()

    try:
        receipt_lines = _resolve_receipt_lines(client, req, order)
        outcome = _apply_receipt_lines(db, req, receipt_lines, source)
        _recompute_rollups(db, req)
        req.error_message = None
        db.commit()
        outcome.status = req.status
        logger.info(
            "Receipt for request %s (order %s): %s applied, %s skipped",
            req.id, req.unleashed_order_number, outcome.applied, outcome.skipped,
        )
        return outcome
    except Exception as exc:  # noqa: BLE001 - record and allow retry, never double-count
        db.rollback()
        req = db.get(DailyRequest, request_id)
        req.status = RequestStatus.RECEIPT_ERROR
        req.error_message = str(exc)[:1000]
        db.commit()
        logger.exception("Receipt processing failed for request %s", request_id)
        return ReceiptOutcome(status=RequestStatus.RECEIPT_ERROR, messages=[str(exc)])


def _resolve_receipt_lines(
    client: UnleashedClient, req: DailyRequest, order: dict
) -> list[ReceiptLine]:
    use_shipments = settings.unleashed_receipt_use_shipments
    if use_shipments and req.unleashed_order_number:
        shipments = client.get_shipments_for_order(req.unleashed_order_number)
        lines = _collect_shipment_lines(shipments)
        if lines:
            return lines
        if not settings.unleashed_receipt_fallback_to_order:
            return []  # wait for shipments to appear
    # Fallback / shipments disabled: trust completed order line quantities.
    return _collect_order_lines(order)


def _apply_receipt_lines(
    db: Session, req: DailyRequest, receipt_lines: list[ReceiptLine], source: str
) -> ReceiptOutcome:
    outcome = ReceiptOutcome(status=req.status)

    # Map product_code -> product_id for this store's active inventory.
    inv_rows = db.execute(
        select(Product.product_code, Product.id, StoreInventory.id)
        .join(StoreInventory, StoreInventory.product_id == Product.id)
        .where(StoreInventory.store_id == req.store_id)
    ).all()
    code_to_product = {code: pid for (code, pid, _inv_id) in inv_rows}

    # Map sales_order_line_number -> DailyRequestLine for precise matching.
    line_by_number = {
        ln.sales_order_line_number: ln for ln in req.lines if ln.sales_order_line_number
    }
    line_by_product = {ln.product_id: ln for ln in req.lines}

    for rl in receipt_lines:
        product_id = code_to_product.get(rl.product_code)
        if product_id is None:
            outcome.messages.append(f"no local product/inventory for {rl.product_code}; skipped")
            outcome.skipped += 1
            continue

        drl: DailyRequestLine | None = None
        if rl.sales_order_line_number is not None:
            drl = line_by_number.get(rl.sales_order_line_number)
        if drl is None:
            drl = line_by_product.get(product_id)

        result = inventory_service.apply_receipt_line(
            db,
            store_id=req.store_id,
            product_id=product_id,
            quantity=rl.quantity,
            idempotency_key=rl.idempotency_key,
            source=source,
            daily_request_id=req.id,
            daily_request_line_id=drl.id if drl else None,
            unleashed_sales_order_guid=req.unleashed_sales_order_guid,
            unleashed_order_number=req.unleashed_order_number,
            unleashed_shipment_guid=rl.shipment_guid,
            note=f"Receipt {rl.product_code} qty {rl.quantity:g}",
        )
        if result.applied:
            outcome.applied += 1
        else:
            outcome.skipped += 1
    return outcome
