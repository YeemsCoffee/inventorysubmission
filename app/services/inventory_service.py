"""Inventory service — the ONLY place `StoreInventory.current_count` changes.

Every mutation is transaction-safe and writes an InventoryTransaction ledger row.
No route or other module should edit current_count directly.

Rules enforced here:
  * Employee removals decrease the count (STORE_REMOVAL, delta < 0).
  * Fulfilled Unleashed orders increase the count (UNLEASHED_RECEIPT, delta > 0),
    and are idempotent via a unique idempotency_key.
  * Manager/admin corrections (COUNT_ADJUSTMENT) are explicit and logged.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..config import get_settings
from ..enums import TransactionType
from ..models import InventoryTransaction, StoreInventory

settings = get_settings()

# Employees may undo their own scan for this long; after that a manager
# correction is the honest way to fix the count.
UNDO_WINDOW_MINUTES = 15


class InventoryError(Exception):
    pass


@dataclass
class MutationResult:
    applied: bool
    quantity_before: float
    quantity_after: float
    transaction: InventoryTransaction | None
    reason: str = "applied"


def get_inventory_by_tag(db: Session, tag_id: str) -> StoreInventory | None:
    return db.execute(
        select(StoreInventory).where(
            StoreInventory.tag_id == tag_id, StoreInventory.active.is_(True)
        )
    ).scalar_one_or_none()


def _lock_inventory(db: Session, store_id: int, product_id: int) -> StoreInventory:
    stmt = select(StoreInventory).where(
        StoreInventory.store_id == store_id,
        StoreInventory.product_id == product_id,
    )
    # Row lock on Postgres; SQLite serialises writes so the unique constraint and
    # single-writer behaviour provide the guarantee there.
    if not settings.is_sqlite:
        stmt = stmt.with_for_update()
    inv = db.execute(stmt).scalar_one_or_none()
    if inv is None:
        raise InventoryError(f"No StoreInventory for store={store_id} product={product_id}")
    return inv


def _apply(
    db: Session,
    *,
    inv: StoreInventory,
    delta: float,
    transaction_type: str,
    source: str,
    employee_id: int | None = None,
    note: str | None = None,
    daily_request_id: int | None = None,
    daily_request_line_id: int | None = None,
    unleashed_sales_order_guid: str | None = None,
    unleashed_order_number: str | None = None,
    unleashed_shipment_guid: str | None = None,
    idempotency_key: str | None = None,
) -> InventoryTransaction:
    before = inv.current_count
    after = before + delta
    txn = InventoryTransaction(
        store_id=inv.store_id,
        product_id=inv.product_id,
        transaction_type=transaction_type,
        quantity_delta=delta,
        quantity_before=before,
        quantity_after=after,
        source=source,
        employee_id=employee_id,
        daily_request_id=daily_request_id,
        daily_request_line_id=daily_request_line_id,
        unleashed_sales_order_guid=unleashed_sales_order_guid,
        unleashed_order_number=unleashed_order_number,
        unleashed_shipment_guid=unleashed_shipment_guid,
        idempotency_key=idempotency_key,
        note=note,
        timestamp=datetime.utcnow(),
    )
    db.add(txn)
    inv.current_count = after
    inv.last_updated_at = datetime.utcnow()
    return txn


def record_removal(
    db: Session,
    *,
    inventory: StoreInventory,
    quantity: float,
    employee_id: int | None = None,
    source: str = "scan",
    note: str | None = None,
) -> MutationResult:
    """Employee removed `quantity` from back storage for café use (delta < 0)."""
    if quantity <= 0:
        raise InventoryError("Removal quantity must be positive")
    inv = _lock_inventory(db, inventory.store_id, inventory.product_id)
    txn = _apply(
        db,
        inv=inv,
        delta=-abs(quantity),
        transaction_type=TransactionType.STORE_REMOVAL,
        source=source,
        employee_id=employee_id,
        note=note,
    )
    db.commit()
    db.refresh(txn)
    db.refresh(inv)
    return MutationResult(True, txn.quantity_before, txn.quantity_after, txn)


def undo_removal(
    db: Session,
    *,
    transaction_id: int,
    inventory: StoreInventory,
) -> MutationResult:
    """Reverse a just-made STORE_REMOVAL (employee tapped Undo).

    Idempotent (a removal can be undone at most once), restricted to the item
    the tag points at, and only within UNDO_WINDOW_MINUTES of the removal.
    """
    original = db.get(InventoryTransaction, transaction_id)
    if original is None or original.transaction_type != TransactionType.STORE_REMOVAL:
        raise InventoryError("That removal can't be found.")
    if original.store_id != inventory.store_id or original.product_id != inventory.product_id:
        raise InventoryError("That removal belongs to a different item.")
    if original.quantity_delta >= 0:
        raise InventoryError("That entry isn't an undoable removal.")
    if datetime.utcnow() - original.timestamp > timedelta(minutes=UNDO_WINDOW_MINUTES):
        raise InventoryError("Too late to undo — ask a manager to correct the count.")

    key = f"undo-removal:{original.id}"
    existing = db.execute(
        select(InventoryTransaction).where(InventoryTransaction.idempotency_key == key)
    ).scalar_one_or_none()
    if existing is not None:
        return MutationResult(
            False, existing.quantity_before, existing.quantity_after, existing, reason="duplicate"
        )

    inv = _lock_inventory(db, original.store_id, original.product_id)
    txn = _apply(
        db,
        inv=inv,
        delta=abs(original.quantity_delta),
        transaction_type=TransactionType.SCAN_UNDO,
        source="scan-undo",
        employee_id=original.employee_id,
        note=f"Undo of removal #{original.id}",
        idempotency_key=key,
    )
    try:
        db.commit()
    except IntegrityError:
        # Double-tap race: someone else undid it first — report as duplicate.
        db.rollback()
        existing = db.execute(
            select(InventoryTransaction).where(InventoryTransaction.idempotency_key == key)
        ).scalar_one_or_none()
        return MutationResult(
            False,
            existing.quantity_before if existing else 0,
            existing.quantity_after if existing else 0,
            existing,
            reason="duplicate",
        )
    db.refresh(txn)
    return MutationResult(True, txn.quantity_before, txn.quantity_after, txn)


def record_count_adjustment(
    db: Session,
    *,
    inventory: StoreInventory,
    new_count: float,
    employee_id: int | None = None,
    note: str | None = None,
    source: str = "manager",
) -> MutationResult:
    """Manager/admin sets an absolute corrected count. Logged as COUNT_ADJUSTMENT."""
    inv = _lock_inventory(db, inventory.store_id, inventory.product_id)
    delta = new_count - inv.current_count
    txn = _apply(
        db,
        inv=inv,
        delta=delta,
        transaction_type=TransactionType.COUNT_ADJUSTMENT,
        source=source,
        employee_id=employee_id,
        note=note or "Manual count correction",
    )
    db.commit()
    db.refresh(txn)
    return MutationResult(True, txn.quantity_before, txn.quantity_after, txn)


def apply_receipt_line(
    db: Session,
    *,
    store_id: int,
    product_id: int,
    quantity: float,
    idempotency_key: str,
    source: str,
    daily_request_id: int | None = None,
    daily_request_line_id: int | None = None,
    unleashed_sales_order_guid: str | None = None,
    unleashed_order_number: str | None = None,
    unleashed_shipment_guid: str | None = None,
    note: str | None = None,
) -> MutationResult:
    """Idempotently add a fulfilled/shipped quantity to local inventory.

    Safe under duplicate webhooks, duplicate polls and retries: the unique
    `idempotency_key` ensures a given shipment/order line is applied at most once.
    """
    if quantity <= 0:
        # Nothing shipped on this line — record nothing, report as skipped.
        return MutationResult(False, 0, 0, None, reason="zero_quantity")

    # Fast path: already applied?
    existing = db.execute(
        select(InventoryTransaction).where(
            InventoryTransaction.idempotency_key == idempotency_key
        )
    ).scalar_one_or_none()
    if existing is not None:
        return MutationResult(
            False, existing.quantity_before, existing.quantity_after, existing, reason="duplicate"
        )

    inv = _lock_inventory(db, store_id, product_id)
    txn = _apply(
        db,
        inv=inv,
        delta=abs(quantity),
        transaction_type=TransactionType.UNLEASHED_RECEIPT,
        source=source,
        note=note,
        daily_request_id=daily_request_id,
        daily_request_line_id=daily_request_line_id,
        unleashed_sales_order_guid=unleashed_sales_order_guid,
        unleashed_order_number=unleashed_order_number,
        unleashed_shipment_guid=unleashed_shipment_guid,
        idempotency_key=idempotency_key,
    )
    try:
        db.commit()
    except IntegrityError:
        # Lost a race to a concurrent receipt for the same key — treat as duplicate.
        db.rollback()
        existing = db.execute(
            select(InventoryTransaction).where(
                InventoryTransaction.idempotency_key == idempotency_key
            )
        ).scalar_one_or_none()
        return MutationResult(
            False,
            existing.quantity_before if existing else 0,
            existing.quantity_after if existing else 0,
            existing,
            reason="duplicate",
        )
    db.refresh(txn)
    return MutationResult(True, txn.quantity_before, txn.quantity_after, txn)


def record_audit(
    db: Session,
    *,
    store_id: int,
    product_id: int,
    transaction_type: str,
    source: str,
    daily_request_id: int | None = None,
    daily_request_line_id: int | None = None,
    unleashed_sales_order_guid: str | None = None,
    unleashed_order_number: str | None = None,
    unleashed_shipment_guid: str | None = None,
    note: str | None = None,
    employee_id: int | None = None,
) -> InventoryTransaction:
    """Zero-delta ledger marker (request generated/submitted/override/sync error).

    Keeps a complete movement history without touching current_count. Caller
    is responsible for committing (these are usually part of a larger unit).
    """
    txn = InventoryTransaction(
        store_id=store_id,
        product_id=product_id,
        transaction_type=transaction_type,
        quantity_delta=0,
        quantity_before=0,
        quantity_after=0,
        source=source,
        employee_id=employee_id,
        daily_request_id=daily_request_id,
        daily_request_line_id=daily_request_line_id,
        unleashed_sales_order_guid=unleashed_sales_order_guid,
        unleashed_order_number=unleashed_order_number,
        unleashed_shipment_guid=unleashed_shipment_guid,
        note=note,
        timestamp=datetime.utcnow(),
    )
    db.add(txn)
    return txn
