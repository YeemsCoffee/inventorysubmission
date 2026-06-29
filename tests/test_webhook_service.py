"""Webhook intake: dedupe on notification id, then run the idempotent receipt."""
from __future__ import annotations

from app.models import WebhookEvent
from app.services import receipt_service, request_service, webhook_service
from tests.conftest import FakeUnleashedClient


def _submitted(db, inventory, client):
    req = request_service.generate_daily_request(db, store_id=inventory.store_id)
    return request_service.submit_to_unleashed(db, request_id=req.id, client=client)


def test_duplicate_webhook_is_ignored(db, inventory):
    shipments = [{
        "Guid": "ship-1", "OrderNumber": "SO-TEST-001",
        "ShipmentLines": [{"Guid": "sl-1", "Product": {"ProductCode": "OATMILK"},
                           "SalesOrderLineNumber": 1, "ShipmentQty": 6}],
    }]
    client = FakeUnleashedClient(order_status="Completed", shipments=shipments)
    receipt_service.settings.unleashed_receipt_use_shipments = True
    req = _submitted(db, inventory, client)

    payload = {
        "EventNotificationId": "evt-123",
        "EventType": "SalesOrder.Completed",
        "Data": {"Guid": req.unleashed_sales_order_guid},
    }
    r1 = webhook_service.handle_webhook(db, payload, client=client)
    r2 = webhook_service.handle_webhook(db, payload, client=client)  # duplicate delivery

    assert r1["status"] == "processed"
    assert r2["status"] == "duplicate"
    assert db.query(WebhookEvent).count() == 1
    db.refresh(inventory)
    assert inventory.current_count == 24  # applied once only
