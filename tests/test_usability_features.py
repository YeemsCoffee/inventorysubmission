"""Scan undo, bulk par editing, and the needs-attention surface."""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.enums import RequestStatus, Role, TransactionType
from app.main import app
from app.models import DailyRequest, InventoryTransaction, Store, StoreInventory, User
from app.security import hash_password
from app.services import attention_service, inventory_service


@pytest.fixture()
def client(db):
    return TestClient(app)


@pytest.fixture()
def admin_client(db):
    u = User(name="Boss", email="boss@test.local", role=Role.ADMIN,
             password_hash=hash_password("pw"), active=True)
    db.add(u)
    db.commit()
    c = TestClient(app)
    r = c.post("/login", data={"email": u.email, "password": "pw"}, follow_redirects=False)
    assert r.status_code == 303
    return c


# ---------------- scan undo ----------------

def _scan(client, tag, qty=3):
    r = client.post(f"/api/scan/{tag}", data={"quantity": qty})
    body = r.json()
    assert body["ok"], body
    return body


def test_undo_restores_count_and_writes_ledger(client, db, inventory):
    body = _scan(client, inventory.tag_id, qty=3)
    assert body["new_count"] == 15  # 18 - 3
    assert body["txn_id"] and body["undo_token"]

    r = client.post(
        f"/api/scan/{inventory.tag_id}/undo",
        data={"txn_id": body["txn_id"], "token": body["undo_token"]},
    )
    res = r.json()
    assert res["ok"] and res["new_count"] == 18
    db.expire_all()
    assert db.get(StoreInventory, inventory.id).current_count == 18
    undo_rows = db.query(InventoryTransaction).filter_by(
        transaction_type=TransactionType.SCAN_UNDO
    ).all()
    assert len(undo_rows) == 1 and undo_rows[0].quantity_delta == 3


def test_undo_twice_is_idempotent(client, db, inventory):
    body = _scan(client, inventory.tag_id, qty=2)
    for _ in range(2):
        r = client.post(
            f"/api/scan/{inventory.tag_id}/undo",
            data={"txn_id": body["txn_id"], "token": body["undo_token"]},
        )
        assert r.json()["ok"]
    db.expire_all()
    assert db.get(StoreInventory, inventory.id).current_count == 18  # not 20
    assert db.query(InventoryTransaction).filter_by(
        transaction_type=TransactionType.SCAN_UNDO
    ).count() == 1


def test_undo_rejects_bad_token(client, db, inventory):
    body = _scan(client, inventory.tag_id, qty=1)
    r = client.post(
        f"/api/scan/{inventory.tag_id}/undo",
        data={"txn_id": body["txn_id"], "token": "forged-token-000000000000"},
    )
    res = r.json()
    assert not res["ok"] and "not valid" in res["error"]
    db.expire_all()
    assert db.get(StoreInventory, inventory.id).current_count == 17  # unchanged


def test_undo_rejects_expired_window(client, db, inventory):
    body = _scan(client, inventory.tag_id, qty=1)
    txn = db.get(InventoryTransaction, body["txn_id"])
    txn.timestamp = datetime.utcnow() - timedelta(minutes=inventory_service.UNDO_WINDOW_MINUTES + 1)
    db.commit()

    r = client.post(
        f"/api/scan/{inventory.tag_id}/undo",
        data={"txn_id": body["txn_id"], "token": body["undo_token"]},
    )
    res = r.json()
    assert not res["ok"] and "Too late" in res["error"]


def test_undo_rejects_other_items_transaction(client, db, inventory, store):
    from app.models import Product

    other_product = Product(product_code="OTHER", display_name="Other", unit_of_measure="EA",
                            case_quantity=1, active=True)
    db.add(other_product)
    db.flush()
    other_inv = StoreInventory(store_id=store.id, product_id=other_product.id, current_count=5,
                               par_level=5, minimum_level=1, tag_id="KTOWN-OTHER", active=True)
    db.add(other_inv)
    db.commit()

    body = _scan(client, inventory.tag_id, qty=1)  # removal on OATMILK
    # Try to undo the OATMILK removal through the OTHER item's tag.
    r = client.post(
        "/api/scan/KTOWN-OTHER/undo",
        data={"txn_id": body["txn_id"], "token": body["undo_token"]},
    )
    res = r.json()
    assert not res["ok"] and "different item" in res["error"]


def test_no_js_undo_flow(client, db, inventory):
    r = client.post(f"/scan/{inventory.tag_id}", data={"quantity": 2})
    assert r.status_code == 200 and "Undo this" in r.text
    # Pull txn id + token out of the rendered form.
    import re

    txn_id = re.search(r'name="txn_id" value="(\d+)"', r.text).group(1)
    token = re.search(r'name="token" value="([0-9a-f]+)"', r.text).group(1)
    r = client.post(f"/scan/{inventory.tag_id}/undo", data={"txn_id": txn_id, "token": token})
    assert r.status_code == 200 and "Undone" in r.text
    db.expire_all()
    assert db.get(StoreInventory, inventory.id).current_count == 18


# ---------------- bulk par editing ----------------

def test_bulk_pars_updates_only_valid_rows(admin_client, db, inventory, store, product):
    from app.models import Product

    p2 = Product(product_code="P2", display_name="P2", unit_of_measure="EA", case_quantity=1, active=True)
    db.add(p2)
    db.flush()
    inv2 = StoreInventory(store_id=store.id, product_id=p2.id, current_count=0,
                          par_level=0, minimum_level=0, tag_id="KTOWN-P2", active=True)
    db.add(inv2)
    db.commit()
    db.refresh(inv2)

    r = admin_client.post(
        "/admin/inventory/bulk-pars",
        data={
            "store_id": store.id,
            f"par_{inventory.id}": "30",
            f"min_{inventory.id}": "5",
            f"par_{inv2.id}": "12",
            f"min_{inv2.id}": "-4",      # negative -> invalid, unchanged
            "par_99999": "7",             # unknown row -> invalid
            "par_abc": "7",               # junk key -> invalid
        },
        follow_redirects=False,
    )
    assert r.status_code == 303 and "2+item" in r.headers["location"].replace("%20", "+")
    db.expire_all()
    assert db.get(StoreInventory, inventory.id).par_level == 30
    assert db.get(StoreInventory, inventory.id).minimum_level == 5
    assert db.get(StoreInventory, inv2.id).par_level == 12
    assert db.get(StoreInventory, inv2.id).minimum_level == 0  # negative rejected


def test_bulk_pars_rejects_non_finite_values(admin_client, db, inventory, store):
    r = admin_client.post(
        "/admin/inventory/bulk-pars",
        data={"store_id": store.id, f"par_{inventory.id}": "nan", f"min_{inventory.id}": "inf"},
        follow_redirects=False,
    )
    assert r.status_code == 303 and "0+item" in r.headers["location"].replace("%20", "+")
    db.expire_all()
    assert db.get(StoreInventory, inventory.id).par_level == 24  # untouched


def test_bulk_pars_accepts_fractional_values(admin_client, db, inventory, store):
    r = admin_client.post(
        "/admin/inventory/bulk-pars",
        data={"store_id": store.id, f"par_{inventory.id}": "2.5", f"min_{inventory.id}": "0.5"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    db.expire_all()
    assert db.get(StoreInventory, inventory.id).par_level == 2.5
    assert db.get(StoreInventory, inventory.id).minimum_level == 0.5


def test_bulk_pars_ignores_rows_of_other_stores(admin_client, db, inventory, store):
    other = Store(store_code="G", store_name="Gardena", unleashed_customer_code="G", active=True)
    db.add(other)
    db.commit()

    r = admin_client.post(
        "/admin/inventory/bulk-pars",
        data={"store_id": other.id, f"par_{inventory.id}": "99"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    db.expire_all()
    assert db.get(StoreInventory, inventory.id).par_level == 24  # untouched


# ---------------- attention surface ----------------

def test_attention_collects_errors_and_stale_orders(db, store):
    db.add(DailyRequest(store_id=store.id, status=RequestStatus.SYNC_ERROR,
                        error_message="boom"))
    db.add(DailyRequest(store_id=store.id, status=RequestStatus.RECEIPT_ERROR,
                        error_message="receipt boom"))
    db.add(DailyRequest(store_id=store.id, status=RequestStatus.SUBMITTED,
                        submitted_at=datetime.utcnow() - timedelta(days=5)))
    db.add(DailyRequest(store_id=store.id, status=RequestStatus.SUBMITTED,
                        submitted_at=datetime.utcnow()))  # fresh -> not flagged
    db.add(DailyRequest(store_id=store.id, status=RequestStatus.RECEIVED))
    db.commit()

    att = attention_service.get_attention(db)
    assert att["total"] == 3 and att["truncated"] is False
    kinds = {i["kind"] for i in att["items"]}
    assert kinds == {"sync_error", "receipt_error", "stale"}


def test_attention_is_bounded_under_bulk_failures(db, store):
    for i in range(attention_service.MAX_ITEMS + 5):
        db.add(DailyRequest(store_id=store.id, status=RequestStatus.SYNC_ERROR, error_message=f"e{i}"))
    db.commit()
    att = attention_service.get_attention(db)
    assert att["total"] == attention_service.MAX_ITEMS
    assert att["truncated"] is True


def test_attention_banner_renders_on_every_staff_page(admin_client, db, store):
    db.add(DailyRequest(store_id=store.id, status=RequestStatus.SYNC_ERROR, error_message="bad customer"))
    db.commit()
    # Global via base.html: home page AND working pages, not just the dashboard.
    for url in ["/admin", "/manager/inventory", "/admin/settings", "/warehouse/requests"]:
        r = admin_client.get(url)
        assert r.status_code == 200, url
        assert "Needs attention" in r.text and "failed to submit" in r.text, url


def test_no_banner_when_all_clear(admin_client, db, store):
    r = admin_client.get("/admin")
    assert r.status_code == 200 and "Needs attention" not in r.text
