"""Completion -> receipt: actual quantities, idempotent, with shipment + fallback."""
from __future__ import annotations

from app.enums import RequestStatus
from app.services import receipt_service, request_service
from tests.conftest import FakeUnleashedClient


def _submitted_request(db, inventory, client):
    req = request_service.generate_daily_request(db, store_id=inventory.store_id)
    return request_service.submit_to_unleashed(db, request_id=req.id, client=client)


def test_receipt_from_shipments_adds_actual_shipped(db, inventory):
    # Requested 6, but warehouse shipped only 5 -> local count rises by 5, not 6.
    shipments = [{
        "Guid": "ship-1",
        "OrderNumber": "SO-TEST-001",
        "ShipmentLines": [
            {"Guid": "sl-1", "Product": {"ProductCode": "OATMILK"}, "SalesOrderLineNumber": 1, "ShipmentQty": 5}
        ],
    }]
    client = FakeUnleashedClient(order_status="Completed", shipments=shipments)
    receipt_service.settings.unleashed_receipt_use_shipments = True
    req = _submitted_request(db, inventory, client)

    outcome = receipt_service.process_completion(db, request_id=req.id, client=client)
    db.refresh(inventory)
    assert inventory.current_count == 23  # 18 + 5
    assert outcome.applied == 1
    db.refresh(req)
    assert req.completed_at is not None


def test_receipt_is_idempotent_across_repeats(db, inventory):
    shipments = [{
        "Guid": "ship-1", "OrderNumber": "SO-TEST-001",
        "ShipmentLines": [
            {"Guid": "sl-1", "Product": {"ProductCode": "OATMILK"}, "SalesOrderLineNumber": 1, "ShipmentQty": 6}
        ],
    }]
    client = FakeUnleashedClient(order_status="Completed", shipments=shipments)
    receipt_service.settings.unleashed_receipt_use_shipments = True
    req = _submitted_request(db, inventory, client)

    receipt_service.process_completion(db, request_id=req.id, client=client)
    receipt_service.process_completion(db, request_id=req.id, client=client)  # webhook + poll
    receipt_service.process_completion(db, request_id=req.id, client=client)
    db.refresh(inventory)
    assert inventory.current_count == 24  # 18 + 6, applied exactly once
    db.refresh(req)
    assert req.status == RequestStatus.RECEIVED
    assert req.received_at is not None


def test_fallback_to_order_lines_when_no_shipments(db, inventory):
    client = FakeUnleashedClient(
        order_status="Completed",
        shipments=[],  # none available
        order_lines=[{"LineNumber": 1, "Product": {"ProductCode": "OATMILK"}, "OrderQuantity": 6}],
    )
    receipt_service.settings.unleashed_receipt_use_shipments = True
    receipt_service.settings.unleashed_receipt_fallback_to_order = True
    req = _submitted_request(db, inventory, client)

    receipt_service.process_completion(db, request_id=req.id, client=client)
    db.refresh(inventory)
    assert inventory.current_count == 24  # 18 + 6 from order line fallback


def test_not_completed_does_not_receive(db, inventory):
    client = FakeUnleashedClient(order_status="Placed")
    req = _submitted_request(db, inventory, client)
    receipt_service.process_completion(db, request_id=req.id, client=client)
    db.refresh(inventory)
    assert inventory.current_count == 18  # unchanged
    db.refresh(req)
    assert req.status == RequestStatus.SUBMITTED
