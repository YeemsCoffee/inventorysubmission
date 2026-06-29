"""Live Unleashed connectivity diagnostic.

Run this in an environment that can reach api.unleashedsoftware.com with your
real credentials (your laptop, server, or CI) to verify the HMAC signature and
credentials end-to-end:

    UNLEASHED_API_ID=... UNLEASHED_API_KEY=... python -m scripts.check_unleashed
    # or just put them in .env and run:  python -m scripts.check_unleashed

Read-only: it pings, then lists one sales order / customer / product. Exits
non-zero on failure so it can gate a deploy. Secrets are never printed.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import Settings  # noqa: E402
from app.integrations.unleashed import UnleashedClient, UnleashedError  # noqa: E402


def _ok(msg: str) -> None:
    print(f"  ✓ {msg}")


def _fail(msg: str) -> None:
    print(f"  ✗ {msg}")


def run() -> int:
    s = Settings()
    print("Unleashed connectivity check")
    print(f"  API URL: {s.unleashed_api_url}")
    print(f"  Credentials: {'set' if s.unleashed_configured else 'NOT set'}")
    if not s.unleashed_configured:
        _fail("UNLEASHED_API_ID / UNLEASHED_API_KEY are not configured.")
        return 2

    client = UnleashedClient(settings=s)

    try:
        client.ping()
        _ok("Authenticated (HMAC signature accepted).")
    except UnleashedError as exc:
        _fail(f"Auth/connectivity failed: {exc}")
        return 1

    try:
        orders = client.list_sales_orders(page=1, page_size=1)
        pagination = orders.get("Pagination", {}) if isinstance(orders, dict) else {}
        total = pagination.get("NumberOfItems", "unknown")
        _ok(f"Listed sales orders (total in account: {total}).")
    except UnleashedError as exc:
        _fail(f"List sales orders failed: {exc}")
        return 1

    try:
        customers = client.get_customers(pageSize=1)
        _ok(f"Listed customers ({len(customers)} returned on page 1).")
    except UnleashedError as exc:
        _fail(f"List customers failed: {exc}")
        return 1

    try:
        products = client.get_products(pageSize=1)
        _ok(f"Listed products ({len(products)} returned on page 1).")
    except UnleashedError as exc:
        _fail(f"List products failed: {exc}")
        return 1

    print("All checks passed. ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
