"""LIVE Unleashed integration tests (real API).

These are excluded from the default `pytest` run (see pyproject `addopts`) and
only execute when you opt in AND real credentials are present:

    # read-only checks (auth/signature + a couple of GETs)
    UNLEASHED_API_ID=... UNLEASHED_API_KEY=... pytest -m integration

    # also exercise a create->read round-trip (creates ONE reusable Parked draft)
    UNLEASHED_SANDBOX_WRITE=true \
    UNLEASHED_TEST_CUSTOMER_CODE=KTOWN \
    UNLEASHED_TEST_PRODUCT_CODE=OATMILK \
    pytest -m integration

If credentials are missing the tests SKIP (so CI without secrets stays green).
The write test uses a FIXED Guid, so re-running updates the same draft order
instead of piling up new ones — which also demonstrates Guid-first idempotency
against the live API.
"""
from __future__ import annotations

import os

import pytest

from app.config import Settings
from app.integrations.unleashed import UnleashedClient, UnleashedError

pytestmark = pytest.mark.integration

# Stable Guid for the (opt-in) live write round-trip so repeats are idempotent.
ITEST_GUID = "c0ffee00-0000-4000-8000-000000000001"
ITEST_SOURCE_ID = "CAFEAPP-ITEST"


def _settings() -> Settings:
    # Fresh Settings() reads the current env/.env (not the cached unit-test config).
    s = Settings()
    if not s.unleashed_configured:
        pytest.skip("Unleashed credentials not set — skipping live integration tests")
    return s


def _client(s: Settings) -> UnleashedClient:
    return UnleashedClient(settings=s)


def test_ping_authenticates_against_live_api():
    """A successful ping proves the HMAC signature + credentials are correct."""
    s = _settings()
    assert _client(s).ping() is True


def test_list_sales_orders_readonly():
    s = _settings()
    data = _client(s).list_sales_orders(page=1, page_size=1)
    assert isinstance(data, dict)
    # Unleashed list responses carry Items and/or Pagination metadata.
    assert "Items" in data or "Pagination" in data


def test_get_customers_readonly():
    s = _settings()
    customers = _client(s).get_customers(pageSize=1)
    assert isinstance(customers, list)


@pytest.mark.skipif(
    os.getenv("UNLEASHED_SANDBOX_WRITE", "").lower() not in ("1", "true", "yes"),
    reason="set UNLEASHED_SANDBOX_WRITE=true to run the live create->read round-trip",
)
def test_create_and_read_sales_order_roundtrip():
    s = _settings()
    customer = os.getenv("UNLEASHED_TEST_CUSTOMER_CODE")
    product = os.getenv("UNLEASHED_TEST_PRODUCT_CODE")
    if not (customer and product):
        pytest.skip("set UNLEASHED_TEST_CUSTOMER_CODE and UNLEASHED_TEST_PRODUCT_CODE")

    client = _client(s)
    payload = {
        "Guid": ITEST_GUID,
        "OrderStatus": s.unleashed_create_order_status,  # Parked (harmless draft)
        "Customer": {"CustomerCode": customer},
        "Warehouse": {"WarehouseCode": s.unleashed_fulfill_warehouse_code},
        "SourceId": ITEST_SOURCE_ID,
        "SalesOrderLines": [
            {"LineNumber": 1, "Product": {"ProductCode": product}, "OrderQuantity": 1}
        ],
        "Comments": "cafe-inventory integration test — safe to delete",
    }
    if s.unleashed_default_currency:
        payload["Currency"] = {"CurrencyCode": s.unleashed_default_currency}
    if s.unleashed_default_tax_code:
        payload["Tax"] = {"TaxCode": s.unleashed_default_tax_code}

    try:
        created = client.create_sales_order(ITEST_GUID, payload)
    except UnleashedError as exc:
        pytest.fail(
            "Live Sales Order create failed — check the customer/product codes, "
            f"warehouse code, and any required Currency/Tax for your account: {exc}"
        )

    assert isinstance(created, dict)

    # Re-read by Guid and confirm it round-tripped.
    order = client.get_sales_order(ITEST_GUID)
    assert order.get("Guid", "").lower() == ITEST_GUID.lower()
    assert order.get("OrderStatus")  # e.g. "Parked"
    lines = order.get("SalesOrderLines") or []
    assert any((ln.get("Product") or {}).get("ProductCode") == product for ln in lines)
