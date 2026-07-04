"""Daily request generation + idempotent submission to Unleashed."""
from __future__ import annotations

import pytest

from app.enums import RequestStatus
from app.integrations.unleashed import UnleashedError
from app.services import request_service
from tests.conftest import FakeUnleashedClient


class FailingUnleashedClient(FakeUnleashedClient):
    """Simulates Unleashed rejecting the order (e.g. unknown customer code)."""

    def create_sales_order(self, guid, payload):
        raise UnleashedError("Unleashed POST /SalesOrders -> 400: invalid customer")


def test_generate_uses_par_minus_count(db, inventory):
    # par 24, count 18 -> suggested 6
    req = request_service.generate_daily_request(db, store_id=inventory.store_id)
    assert req.status == RequestStatus.DRAFT
    assert len(req.lines) == 1
    line = req.lines[0]
    assert line.suggested_quantity == 6
    assert line.final_requested_quantity == 6


def test_item_at_or_above_par_is_excluded(db, inventory):
    inventory.current_count = 24
    db.commit()
    req = request_service.generate_daily_request(db, store_id=inventory.store_id)
    assert len(req.lines) == 0


def test_regenerate_refreshes_existing_draft(db, inventory):
    r1 = request_service.generate_daily_request(db, store_id=inventory.store_id)
    r2 = request_service.generate_daily_request(db, store_id=inventory.store_id)
    assert r1.id == r2.id  # same draft refreshed, not duplicated


def test_submit_sets_guid_order_number_and_status(db, inventory):
    req = request_service.generate_daily_request(db, store_id=inventory.store_id)
    client = FakeUnleashedClient(order_status="Parked")
    req = request_service.submit_to_unleashed(db, request_id=req.id, client=client)

    assert req.status == RequestStatus.SUBMITTED
    assert req.unleashed_sales_order_guid is not None
    assert req.unleashed_order_number == "SO-TEST-001"
    assert req.unleashed_source_id == f"CAFEAPP-DR-{req.id}"
    # payload created as Parked, mapped to the store's customer, with a warehouse.
    guid, payload = client.created[-1]
    assert payload["OrderStatus"] == "Parked"
    assert payload["Customer"]["CustomerCode"] == "KTOWN"
    assert payload["Warehouse"]["WarehouseCode"]
    assert payload["SalesOrderLines"][0]["OrderQuantity"] == 6


def test_regenerate_after_failed_submit_does_not_violate_ledger_fk(db, inventory):
    """Regression: a failed submit writes SYNC_ERROR ledger rows that reference
    the request lines. Regenerating must not crash deleting those lines
    (IntegrityError on Postgres / FK-enforcing SQLite)."""
    req = request_service.generate_daily_request(db, store_id=inventory.store_id)
    with pytest.raises(UnleashedError):
        request_service.submit_to_unleashed(db, request_id=req.id, client=FailingUnleashedClient())
    assert req.status == RequestStatus.SYNC_ERROR

    # Manager fixes the config and hits "Generate request" again -> refresh, not 500.
    req2 = request_service.generate_daily_request(db, store_id=inventory.store_id)
    assert req2.id == req.id
    assert req2.status == RequestStatus.DRAFT
    assert len(req2.lines) == 1


def test_cancel_submitted_request_stops_polling(db, inventory):
    """Cancelling retires the request locally; the poll sweep no longer touches it."""
    from app.services import sync_service

    req = request_service.generate_daily_request(db, store_id=inventory.store_id)
    client = FakeUnleashedClient(order_status="Parked")
    request_service.submit_to_unleashed(db, request_id=req.id, client=client)

    req = request_service.cancel_request(db, request_id=req.id)
    assert req.status == RequestStatus.CANCELLED

    summary = sync_service.poll_open_requests(db, client=client)
    assert summary["checked"] == 0  # cancelled request is not in the sweep

    # Cancelling twice is a harmless no-op.
    assert request_service.cancel_request(db, request_id=req.id).status == RequestStatus.CANCELLED


def test_cannot_cancel_received_request(db, inventory):
    req = request_service.generate_daily_request(db, store_id=inventory.store_id)
    request_service.submit_to_unleashed(db, request_id=req.id, client=FakeUnleashedClient())
    req.status = RequestStatus.RECEIVED
    db.commit()

    with pytest.raises(request_service.RequestError):
        request_service.cancel_request(db, request_id=req.id)


def test_submit_retry_reuses_same_guid(db, inventory):
    req = request_service.generate_daily_request(db, store_id=inventory.store_id)
    client = FakeUnleashedClient()
    request_service.submit_to_unleashed(db, request_id=req.id, client=client)
    first_guid = req.unleashed_sales_order_guid

    # Force back to a retryable state and submit again.
    req.status = RequestStatus.SYNC_ERROR
    db.commit()
    request_service.submit_to_unleashed(db, request_id=req.id, client=client)
    assert req.unleashed_sales_order_guid == first_guid  # idempotent: no new order guid
