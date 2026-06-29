"""Print every configured scan URL (one per tag) for bulk QR/NFC encoding.

Usage:  python -m scripts.generate_tags
Pipe into any QR batch tool, e.g. each line -> one QR image.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_settings  # noqa: E402
from app.database import SessionLocal  # noqa: E402
from app.models import Product, Store, StoreInventory  # noqa: E402


def run() -> None:
    base = get_settings().base_url.rstrip("/")
    db = SessionLocal()
    try:
        rows = (
            db.query(StoreInventory, Product, Store)
            .join(Product, Product.id == StoreInventory.product_id)
            .join(Store, Store.id == StoreInventory.store_id)
            .filter(StoreInventory.tag_id.isnot(None))
            .order_by(Store.store_name, Product.display_name)
            .all()
        )
        for inv, product, store in rows:
            print(f"{store.store_name}\t{product.display_name}\t{base}/scan/{inv.tag_id}")
    finally:
        db.close()


if __name__ == "__main__":
    run()
