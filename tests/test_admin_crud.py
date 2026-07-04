"""Admin store/user edit + delete over HTTP: guards and deactivate fallbacks."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.enums import RequestStatus, Role
from app.main import app
from app.models import DailyRequest, Store, User
from app.security import hash_password


@pytest.fixture()
def admin(db):
    u = User(
        name="Boss", email="boss@test.local", role=Role.ADMIN,
        password_hash=hash_password("pw"), active=True,
    )
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


@pytest.fixture()
def client(db, admin):
    c = TestClient(app)
    r = c.post("/login", data={"email": admin.email, "password": "pw"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] != "/login"
    return c


def test_delete_unreferenced_store(client, db):
    s = Store(store_code="TMP", store_name="Temp", unleashed_customer_code="TMP", active=True)
    db.add(s)
    db.commit()
    db.refresh(s)

    r = client.post(f"/admin/stores/{s.id}/delete", follow_redirects=False)
    assert r.status_code == 303 and "deleted" in r.headers["location"]
    db.expunge_all()
    assert db.get(Store, s.id) is None


def test_delete_store_with_history_requires_confirmation(client, db, inventory):
    r = client.post(f"/admin/stores/{inventory.store_id}/delete", follow_redirects=False)
    assert f"confirm_delete={inventory.store_id}" in r.headers["location"]
    db.expire_all()
    s = db.get(Store, inventory.store_id)
    assert s is not None and s.active is True  # nothing changed yet


def test_force_delete_store_purges_all_history(client, db, inventory):
    from app.models import DailyRequest, InventoryTransaction, StoreInventory
    from app.services import inventory_service, request_service

    sid = inventory.store_id
    # Real history: a scan removal + a generated draft request with a line.
    inventory_service.record_removal(db, inventory=inventory, quantity=2, source="scan")
    request_service.generate_daily_request(db, store_id=sid)

    r = client.post(f"/admin/stores/{sid}/delete", data={"force": "true"}, follow_redirects=False)
    assert "permanently+deleted" in r.headers["location"] or "permanently%20deleted" in r.headers["location"]
    db.expunge_all()
    assert db.get(Store, sid) is None
    assert db.query(StoreInventory).filter_by(store_id=sid).count() == 0
    assert db.query(InventoryTransaction).filter_by(store_id=sid).count() == 0
    assert db.query(DailyRequest).filter_by(store_id=sid).count() == 0


def test_cannot_delete_own_account(client, db, admin):
    r = client.post(f"/admin/users/{admin.id}/delete", follow_redirects=False)
    assert "err=" in r.headers["location"]
    db.expire_all()
    assert db.get(User, admin.id) is not None


def test_delete_clean_user(client, db):
    emp = User(name="Emp", email="emp@test.local", role=Role.EMPLOYEE, active=True)
    db.add(emp)
    db.commit()
    db.refresh(emp)

    r = client.post(f"/admin/users/{emp.id}/delete", follow_redirects=False)
    assert "deleted" in r.headers["location"]
    db.expunge_all()
    assert db.get(User, emp.id) is None


def test_user_with_history_requires_confirmation_then_detaches(client, db, store):
    mgr = User(
        name="Mgr", email="mgr@test.local", role=Role.STORE_MANAGER,
        password_hash=hash_password("pw"), active=True,
    )
    db.add(mgr)
    db.commit()
    db.refresh(mgr)
    req = DailyRequest(store_id=store.id, status=RequestStatus.SUBMITTED, submitted_by=mgr.id)
    db.add(req)
    db.commit()
    db.refresh(req)

    # First attempt: asks for confirmation, deletes nothing.
    r = client.post(f"/admin/users/{mgr.id}/delete", follow_redirects=False)
    assert f"confirm_delete={mgr.id}" in r.headers["location"]

    # Forced: account gone, request history kept but de-attributed.
    r = client.post(f"/admin/users/{mgr.id}/delete", data={"force": "true"}, follow_redirects=False)
    assert "deleted" in r.headers["location"]
    db.expunge_all()
    assert db.get(User, mgr.id) is None
    kept = db.get(DailyRequest, req.id)
    assert kept is not None and kept.submitted_by is None


def test_cannot_deactivate_last_admin_via_save(client, db, admin):
    r = client.post(
        "/admin/users",
        data={"id": admin.id, "name": admin.name, "email": admin.email, "role": Role.ADMIN, "active": "false"},
        follow_redirects=False,
    )
    assert "err=" in r.headers["location"]
    db.expire_all()
    assert db.get(User, admin.id).active is True


def test_new_product_assigned_to_all_stores(client, db, store):
    from app.models import Product, StoreInventory

    other = Store(store_code="GARDENA", store_name="Gardena", unleashed_customer_code="GARDENA", active=True)
    db.add(other)
    db.commit()

    r = client.post(
        "/admin/products",
        data={
            "product_code": "HAZSYR", "display_name": "Hazelnut Syrup", "unit_of_measure": "BTL",
            "case_quantity": "6", "active": "true",
            "assign_all_stores": "true", "default_par": "8", "default_min": "2",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303 and "msg=" in r.headers["location"]
    db.expunge_all()
    product = db.query(Product).filter_by(product_code="HAZSYR").one()
    rows = db.query(StoreInventory).filter_by(product_id=product.id).all()
    assert len(rows) == 2
    assert {row.tag_id for row in rows} == {"KTOWN-HAZSYR", "GARDENA-HAZSYR"}
    assert all(row.par_level == 8 and row.minimum_level == 2 and row.current_count == 0 for row in rows)


def test_backfill_adds_only_missing_products(client, db, inventory, store):
    from app.models import Product, StoreInventory

    extra = Product(product_code="EXTRA", display_name="Extra", unit_of_measure="EA", case_quantity=1, active=True)
    db.add(extra)
    db.commit()

    r = client.post("/admin/inventory/backfill", data={"store_id": store.id}, follow_redirects=False)
    assert "Added+1" in r.headers["location"] or "Added%201" in r.headers["location"]
    db.expunge_all()
    assert db.query(StoreInventory).filter_by(store_id=store.id).count() == 2

    # Idempotent: running again adds nothing.
    client.post("/admin/inventory/backfill", data={"store_id": store.id}, follow_redirects=False)
    db.expunge_all()
    assert db.query(StoreInventory).filter_by(store_id=store.id).count() == 2


def test_delete_clean_product(client, db):
    from app.models import Product

    p = Product(product_code="GONE", display_name="Gone", unit_of_measure="EA", case_quantity=1, active=True)
    db.add(p)
    db.commit()
    db.refresh(p)

    r = client.post(f"/admin/products/{p.id}/delete", follow_redirects=False)
    assert "deleted" in r.headers["location"]
    db.expunge_all()
    assert db.get(Product, p.id) is None


def test_delete_product_with_history_confirm_then_purge(client, db, inventory, product):
    from app.models import DailyRequest, DailyRequestLine, InventoryTransaction, Product, StoreInventory
    from app.services import inventory_service, request_service

    inventory_service.record_removal(db, inventory=inventory, quantity=1, source="scan")
    req = request_service.generate_daily_request(db, store_id=inventory.store_id)

    # First attempt: confirmation redirect, nothing deleted.
    r = client.post(f"/admin/products/{product.id}/delete", follow_redirects=False)
    assert f"confirm_delete={product.id}" in r.headers["location"]

    r = client.post(f"/admin/products/{product.id}/delete", data={"force": "true"}, follow_redirects=False)
    assert "permanently" in r.headers["location"]
    db.expunge_all()
    assert db.get(Product, product.id) is None
    assert db.query(StoreInventory).filter_by(product_id=product.id).count() == 0
    assert db.query(InventoryTransaction).filter_by(product_id=product.id).count() == 0
    assert db.query(DailyRequestLine).filter_by(product_id=product.id).count() == 0
    assert db.get(DailyRequest, req.id) is not None  # the request record itself survives


def test_delete_inventory_item_keeps_ledger(client, db, inventory):
    from app.models import InventoryTransaction, StoreInventory
    from app.services import inventory_service

    inventory_service.record_removal(db, inventory=inventory, quantity=1, source="scan")
    sid, pid, item_id = inventory.store_id, inventory.product_id, inventory.id

    r = client.post(f"/admin/inventory/{item_id}/delete", follow_redirects=False)
    assert "history+kept" in r.headers["location"] or "history%20kept" in r.headers["location"]
    db.expunge_all()
    assert db.get(StoreInventory, item_id) is None
    assert db.query(InventoryTransaction).filter_by(store_id=sid, product_id=pid).count() == 1


def test_duplicate_email_reports_error_instead_of_500(client, db, admin):
    r = client.post(
        "/admin/users",
        data={"name": "Dupe", "email": admin.email, "role": Role.EMPLOYEE, "active": "true"},
        follow_redirects=False,
    )
    assert r.status_code == 303 and "err=" in r.headers["location"]
