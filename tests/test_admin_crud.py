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


def test_delete_store_with_history_deactivates_instead(client, db, inventory):
    r = client.post(f"/admin/stores/{inventory.store_id}/delete", follow_redirects=False)
    assert "deactivated" in r.headers["location"]
    db.expire_all()
    s = db.get(Store, inventory.store_id)
    assert s is not None and s.active is False


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


def test_user_with_history_is_deactivated_not_deleted(client, db, store):
    mgr = User(
        name="Mgr", email="mgr@test.local", role=Role.STORE_MANAGER,
        password_hash=hash_password("pw"), active=True,
    )
    db.add(mgr)
    db.commit()
    db.refresh(mgr)
    db.add(DailyRequest(store_id=store.id, status=RequestStatus.SUBMITTED, submitted_by=mgr.id))
    db.commit()

    r = client.post(f"/admin/users/{mgr.id}/delete", follow_redirects=False)
    assert "deactivated" in r.headers["location"]
    db.expire_all()
    u = db.get(User, mgr.id)
    assert u is not None and u.active is False


def test_cannot_deactivate_last_admin_via_save(client, db, admin):
    r = client.post(
        "/admin/users",
        data={"id": admin.id, "name": admin.name, "email": admin.email, "role": Role.ADMIN, "active": "false"},
        follow_redirects=False,
    )
    assert "err=" in r.headers["location"]
    db.expire_all()
    assert db.get(User, admin.id).active is True


def test_duplicate_email_reports_error_instead_of_500(client, db, admin):
    r = client.post(
        "/admin/users",
        data={"name": "Dupe", "email": admin.email, "role": Role.EMPLOYEE, "active": "true"},
        follow_redirects=False,
    )
    assert r.status_code == 303 and "err=" in r.headers["location"]
