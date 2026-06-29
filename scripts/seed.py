"""Seed demo data: stores (Ktown, Gardena), products, inventory/par/tags, users.

Usage:  python -m scripts.seed
Idempotent: safe to run repeatedly (upserts by natural keys).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.database import SessionLocal, init_db  # noqa: E402
from app.enums import Role  # noqa: E402
from app.models import Product, Store, StoreInventory, User  # noqa: E402
from app.security import hash_password  # noqa: E402

STORES = [
    {"store_code": "KTOWN", "store_name": "Ktown", "unleashed_customer_code": "KTOWN"},
    {"store_code": "GARDENA", "store_name": "Gardena", "unleashed_customer_code": "GARDENA"},
]

PRODUCTS = [
    {"product_code": "OATMILK", "display_name": "Oat Milk", "category": "Dairy Alt", "unit_of_measure": "EA", "case_quantity": 12},
    {"product_code": "WHOLEMILK", "display_name": "Whole Milk", "category": "Dairy", "unit_of_measure": "EA", "case_quantity": 12},
    {"product_code": "ESPBEAN", "display_name": "Espresso Beans 1kg", "category": "Coffee", "unit_of_measure": "BAG", "case_quantity": 6},
    {"product_code": "CUP12", "display_name": "12oz Cups", "category": "Disposables", "unit_of_measure": "SLEEVE", "case_quantity": 20},
    {"product_code": "VANSYR", "display_name": "Vanilla Syrup", "category": "Syrups", "unit_of_measure": "BTL", "case_quantity": 6},
]

# par/min/initial per store+product, and the scan tag id.
INVENTORY = {
    "KTOWN": [
        ("OATMILK", 24, 6, 18), ("WHOLEMILK", 24, 6, 20), ("ESPBEAN", 12, 3, 8),
        ("CUP12", 10, 2, 7), ("VANSYR", 6, 2, 4),
    ],
    "GARDENA": [
        ("OATMILK", 18, 4, 12), ("WHOLEMILK", 18, 4, 15), ("ESPBEAN", 9, 2, 6),
        ("CUP12", 8, 2, 5), ("VANSYR", 4, 1, 3),
    ],
}

USERS = [
    {"name": "Admin", "email": "admin@yeemscoffee.com", "role": Role.ADMIN, "password": "admin123", "store": None},
    {"name": "Ktown Manager", "email": "manager.ktown@yeemscoffee.com", "role": Role.STORE_MANAGER, "password": "manager123", "store": "KTOWN"},
    {"name": "Warehouse", "email": "warehouse@yeemscoffee.com", "role": Role.WAREHOUSE, "password": "warehouse123", "store": None},
]


def run() -> None:
    init_db()
    db = SessionLocal()
    try:
        stores: dict[str, Store] = {}
        for data in STORES:
            store = db.query(Store).filter_by(store_code=data["store_code"]).one_or_none()
            if store is None:
                store = Store(**data, active=True)
                db.add(store)
            stores[data["store_code"]] = store
        db.flush()

        products: dict[str, Product] = {}
        for data in PRODUCTS:
            p = db.query(Product).filter_by(product_code=data["product_code"]).one_or_none()
            if p is None:
                p = Product(**data, active=True)
                db.add(p)
            products[data["product_code"]] = p
        db.flush()

        for store_code, items in INVENTORY.items():
            store = stores[store_code]
            for product_code, par, minimum, initial in items:
                product = products[product_code]
                tag_id = f"{store_code}-{product_code}"
                inv = (
                    db.query(StoreInventory)
                    .filter_by(store_id=store.id, product_id=product.id)
                    .one_or_none()
                )
                if inv is None:
                    inv = StoreInventory(
                        store_id=store.id, product_id=product.id, current_count=initial,
                        par_level=par, minimum_level=minimum, tag_id=tag_id,
                        storage_location="Back Storage", active=True,
                    )
                    db.add(inv)
                else:
                    inv.par_level, inv.minimum_level, inv.tag_id = par, minimum, tag_id

        for data in USERS:
            u = db.query(User).filter_by(email=data["email"]).one_or_none()
            store = stores.get(data["store"]) if data["store"] else None
            if u is None:
                u = User(
                    name=data["name"], email=data["email"], role=data["role"],
                    password_hash=hash_password(data["password"]),
                    store_id=store.id if store else None, active=True,
                )
                db.add(u)

        db.commit()
        print("Seeded stores:", ", ".join(stores))
        print("Seeded products:", ", ".join(products))
        print("\nLogins (change these!):")
        for u in USERS:
            print(f"  {u['role']:<14} {u['email']}  /  {u['password']}")
        print("\nExample scan URLs:")
        for store_code in INVENTORY:
            print(f"  /scan/{store_code}-OATMILK")
    finally:
        db.close()


if __name__ == "__main__":
    run()
