"""Bulk product import from an Unleashed "Product List" CSV export.

Expected shape (as exported by Unleashed -> Inventory -> Products):

    Product List as of 07/07/2026,,,,,,          <- preamble (skipped)
    Product Code,Product Description,Product Group,Allocated,Bin Location,On Hand,Base Unit
    10000003,Receipt rolls,Cafe Supply,0.0000,W02,22.0000,bundle(s)
    ...

Only Code / Description / Group / Base Unit are used. Warehouse columns
(Allocated, Bin Location, On Hand) are deliberately ignored: store-level
counts in this app never come from Unleashed stock on hand.
"""
from __future__ import annotations

import csv
import io

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Product, Store, StoreInventory


class ImportFormatError(Exception):
    """The uploaded file doesn't look like an Unleashed product export."""


def ensure_inventory_rows(
    db: Session, product: Product, stores: list[Store], *, par: float = 0.0, minimum: float = 0.0
) -> int:
    """Create missing StoreInventory rows for a product (tag auto-named
    {STORE_CODE}-{PRODUCT_CODE}; left blank if that tag is already taken)."""
    created = 0
    for store in stores:
        exists = (
            db.query(StoreInventory.id)
            .filter(StoreInventory.store_id == store.id, StoreInventory.product_id == product.id)
            .first()
        )
        if exists:
            continue
        tag = f"{store.store_code}-{product.product_code}"
        tag_taken = db.query(StoreInventory.id).filter(StoreInventory.tag_id == tag).first()
        db.add(
            StoreInventory(
                store_id=store.id, product_id=product.id, current_count=0,
                par_level=par, minimum_level=minimum,
                tag_id=None if tag_taken else tag,
                storage_location="Back Storage", active=True,
            )
        )
        created += 1
    return created


def import_products_csv(
    db: Session,
    text: str,
    *,
    assign_all_stores: bool = False,
    default_par: float = 0.0,
    default_min: float = 0.0,
) -> dict:
    """Upsert products from CSV text. Returns a summary dict.

    Existing product codes are updated (name/category/unit); new codes are
    created (case_quantity defaults to 1 — not present in the export).
    """
    rows = list(csv.reader(io.StringIO(text)))

    header_idx = None
    header: list[str] = []
    for i, row in enumerate(rows):
        if row and row[0].strip().lower() == "product code":
            header = [c.strip().lower() for c in row]
            header_idx = i
            break
    if header_idx is None:
        raise ImportFormatError(
            "No 'Product Code' header row found — upload the Product List CSV exported from Unleashed."
        )

    col = {name: idx for idx, name in enumerate(header)}
    code_i = col["product code"]
    desc_i = col.get("product description")
    group_i = col.get("product group")
    unit_i = col.get("base unit")

    def cell(row: list[str], idx: int | None) -> str:
        if idx is None or idx >= len(row):
            return ""
        return row[idx].strip()

    created = updated = skipped = 0
    touched: list[Product] = []
    seen: set[str] = set()

    for row in rows[header_idx + 1:]:
        if not row or not any(c.strip() for c in row):
            continue
        code = cell(row, code_i)
        if not code or code in seen:
            skipped += 1
            continue
        seen.add(code)
        name = cell(row, desc_i) or code
        group = cell(row, group_i)
        unit = cell(row, unit_i)

        product = db.execute(select(Product).where(Product.product_code == code)).scalar_one_or_none()
        if product is None:
            product = Product(
                product_code=code, display_name=name, category=group or None,
                unit_of_measure=unit or "EA", case_quantity=1, active=True,
            )
            db.add(product)
            created += 1
        else:
            product.display_name = name
            product.category = group or None
            if unit:
                product.unit_of_measure = unit
            updated += 1
        touched.append(product)

    db.flush()

    assigned = 0
    if assign_all_stores and touched:
        stores = list(db.execute(select(Store).where(Store.active.is_(True))).scalars())
        for product in touched:
            assigned += ensure_inventory_rows(db, product, stores, par=default_par, minimum=default_min)

    db.commit()
    return {"created": created, "updated": updated, "skipped": skipped, "assigned": assigned}
