"""Test fixtures: isolated SQLite DB + a fake Unleashed client (no network)."""
from __future__ import annotations

import os
import tempfile

# Must be set BEFORE importing app modules (engine is built at import time).
_DB_PATH = os.path.join(tempfile.gettempdir(), "cafe_test.db")
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_DB_PATH}")
os.environ.setdefault("SECRET_KEY", "test-secret")
# NOTE: we deliberately do NOT set dummy UNLEASHED_API_ID/KEY here. Unit tests
# always inject FakeUnleashedClient, and leaving these unset lets the live
# integration tests (tests/test_unleashed_integration.py) read the operator's
# real credentials from env/.env without being shadowed by test defaults.

import pytest  # noqa: E402

from app.database import Base, SessionLocal, engine  # noqa: E402
from app.models import Product, Store, StoreInventory  # noqa: E402


@pytest.fixture()
def db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture()
def store(db):
    s = Store(store_code="KTOWN", store_name="Ktown", unleashed_customer_code="KTOWN", active=True)
    db.add(s)
    db.commit()
    db.refresh(s)
    return s


@pytest.fixture()
def product(db):
    p = Product(product_code="OATMILK", display_name="Oat Milk", unit_of_measure="EA", case_quantity=12, active=True)
    db.add(p)
    db.commit()
    db.refresh(p)
    return p


@pytest.fixture()
def inventory(db, store, product):
    inv = StoreInventory(
        store_id=store.id, product_id=product.id, current_count=18,
        par_level=24, minimum_level=6, tag_id="KTOWN-OATMILK", active=True,
    )
    db.add(inv)
    db.commit()
    db.refresh(inv)
    return inv


class FakeUnleashedClient:
    """Records created orders and returns canned reads for completion tests."""

    def __init__(self, order_status="Completed", shipments=None, order_lines=None):
        self.created = []
        self.order_status = order_status
        self.shipments = shipments if shipments is not None else []
        self.order_lines = order_lines or []
        self.last_guid = None
        self.last_payload = None

    def create_sales_order(self, guid, payload):
        self.created.append((guid, payload))
        self.last_guid = guid
        self.last_payload = payload
        return {"Guid": guid, "OrderNumber": "SO-TEST-001", "OrderStatus": payload.get("OrderStatus")}

    def get_sales_order(self, guid):
        return {"Guid": guid, "OrderNumber": "SO-TEST-001", "OrderStatus": self.order_status,
                "SalesOrderLines": self.order_lines}

    def get_shipments_for_order(self, order_number):
        return self.shipments

    def ping(self):
        return True
