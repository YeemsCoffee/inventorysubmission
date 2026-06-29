"""Removals, count adjustments, and idempotent receipts."""
from __future__ import annotations

from app.enums import TransactionType
from app.models import InventoryTransaction
from app.services import inventory_service


def test_removal_decreases_count_and_logs(db, inventory):
    result = inventory_service.record_removal(db, inventory=inventory, quantity=2, source="scan")
    assert result.quantity_before == 18
    assert result.quantity_after == 16
    db.refresh(inventory)
    assert inventory.current_count == 16

    txn = db.query(InventoryTransaction).filter_by(transaction_type=TransactionType.STORE_REMOVAL).one()
    assert txn.quantity_delta == -2
    assert txn.quantity_before == 18 and txn.quantity_after == 16


def test_count_adjustment_sets_absolute_value(db, inventory):
    inventory_service.record_count_adjustment(db, inventory=inventory, new_count=10, note="recount")
    db.refresh(inventory)
    assert inventory.current_count == 10
    txn = db.query(InventoryTransaction).filter_by(transaction_type=TransactionType.COUNT_ADJUSTMENT).one()
    assert txn.quantity_delta == -8


def test_receipt_is_idempotent(db, inventory):
    key = "recv:ship:abc:line:1:prod:OATMILK"
    first = inventory_service.apply_receipt_line(
        db, store_id=inventory.store_id, product_id=inventory.product_id,
        quantity=8, idempotency_key=key, source="poll",
    )
    assert first.applied is True
    db.refresh(inventory)
    assert inventory.current_count == 26  # 18 + 8

    # Same key again -> no double add.
    second = inventory_service.apply_receipt_line(
        db, store_id=inventory.store_id, product_id=inventory.product_id,
        quantity=8, idempotency_key=key, source="poll",
    )
    assert second.applied is False
    assert second.reason == "duplicate"
    db.refresh(inventory)
    assert inventory.current_count == 26  # unchanged

    receipts = db.query(InventoryTransaction).filter_by(transaction_type=TransactionType.UNLEASHED_RECEIPT).count()
    assert receipts == 1
