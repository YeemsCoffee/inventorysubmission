"""Daily request generation + idempotent submission to Unleashed."""
from __future__ import annotations

from app.enums import RequestStatus
from app.services import request_service
from tests.conftest import FakeUnleashedClient


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
