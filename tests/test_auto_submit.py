"""Daily auto-submit sweep + its DB-backed schedule settings."""
from __future__ import annotations

import pytest

from app.enums import RequestStatus
from app.services import auto_submit_service, request_service, settings_service
from tests.conftest import FakeUnleashedClient


def _enable(db, time_str="16:45", tz="America/Los_Angeles"):
    settings_service.save_auto_submit_config(db, enabled=True, time_str=time_str, timezone_str=tz)


def test_disabled_does_nothing(db, inventory):
    client = FakeUnleashedClient()
    summary = auto_submit_service.run_auto_submit(db, client=client)
    assert summary["submitted"] == 0 and client.created == []


def test_generates_and_submits_when_no_request_exists(db, inventory):
    _enable(db)
    client = FakeUnleashedClient(order_status="Parked")
    summary = auto_submit_service.run_auto_submit(db, client=client)
    assert summary == {"submitted": 1, "skipped": 0, "empty": 0, "errors": 0}
    guid, payload = client.created[-1]
    assert payload["SalesOrderLines"][0]["OrderQuantity"] == 6  # par 24 - count 18


def test_respects_manager_curated_draft(db, inventory):
    _enable(db)
    req = request_service.generate_daily_request(
        db, store_id=inventory.store_id, request_date=settings_service.local_today(db)
    )
    request_service.override_line(db, line_id=req.lines[0].id, final_quantity=10, note="manager says 10")

    client = FakeUnleashedClient()
    summary = auto_submit_service.run_auto_submit(db, client=client)
    assert summary["submitted"] == 1
    _guid, payload = client.created[-1]
    assert payload["SalesOrderLines"][0]["OrderQuantity"] == 10  # override kept, not regenerated


def test_refreshes_untouched_draft_to_current_counts(db, inventory):
    _enable(db)
    request_service.generate_daily_request(
        db, store_id=inventory.store_id, request_date=settings_service.local_today(db)
    )
    # More scans happen after the draft was generated.
    from app.services import inventory_service

    inventory_service.record_removal(db, inventory=inventory, quantity=4, source="scan")

    client = FakeUnleashedClient()
    auto_submit_service.run_auto_submit(db, client=client)
    _guid, payload = client.created[-1]
    assert payload["SalesOrderLines"][0]["OrderQuantity"] == 10  # 24 - (18-4)


def test_skips_already_submitted_and_empty_stores(db, inventory):
    _enable(db)
    client = FakeUnleashedClient()
    auto_submit_service.run_auto_submit(db, client=client)
    # Second sweep the same day: already submitted -> skip, no new orders.
    summary = auto_submit_service.run_auto_submit(db, client=client)
    assert summary["submitted"] == 0 and summary["skipped"] == 1
    assert len(client.created) == 1

    # A store at/above par produces no order at all.
    inventory.current_count = inventory.par_level
    db.commit()


def test_skips_store_cancelled_today(db, inventory):
    _enable(db)
    client = FakeUnleashedClient()
    req = request_service.generate_daily_request(
        db, store_id=inventory.store_id, request_date=settings_service.local_today(db)
    )
    request_service.submit_to_unleashed(db, request_id=req.id, client=client)
    request_service.cancel_request(db, request_id=req.id)

    summary = auto_submit_service.run_auto_submit(db, client=client)
    assert summary["submitted"] == 0 and summary["skipped"] == 1
    assert len(client.created) == 1  # nothing new


def test_settings_validation_and_roundtrip(db):
    cfg = settings_service.save_auto_submit_config(
        db, enabled=True, time_str="04:30", timezone_str="America/Chicago"
    )
    assert cfg == {"enabled": True, "time": "04:30", "timezone": "America/Chicago", "hour": 4, "minute": 30}
    assert settings_service.get_auto_submit_config(db)["time"] == "04:30"

    with pytest.raises(ValueError):
        settings_service.save_auto_submit_config(db, enabled=True, time_str="25:99", timezone_str="UTC")
    with pytest.raises(ValueError):
        settings_service.save_auto_submit_config(db, enabled=True, time_str="16:45", timezone_str="Mars/Olympus")
