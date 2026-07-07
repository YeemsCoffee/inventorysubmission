"""Bulk product import from the Unleashed Product List CSV export."""
from __future__ import annotations

import pytest

from app.models import Product, Store, StoreInventory
from app.services.product_import import ImportFormatError, import_products_csv

# Mirrors the real Unleashed export: preamble line, quoted name containing a
# comma, blank group, blank unit, blank numeric cells, trailing blank line.
SAMPLE = """Product List as of 07/07/2026,,,,,,
Product Code,Product Description,Product Group,Allocated,Bin Location,On Hand,Base Unit
10000003,Receipt rolls,Cafe Supply,0.0000,W02,22.0000,bundle(s)
10000014,"Pastry Bag, White",Cafe Supply,0.0000,,0.0000,box(es)
30000018,Sugar in the raw,,0.0000,W01,3.0000,box(es)
30000042,Sugar Tub,Raw Material,,,,EA
20000008,Splenda,Consumables,0.0000,W01,2.0000,
"""


def test_import_creates_products_with_mapped_fields(db):
    result = import_products_csv(db, SAMPLE)
    assert result == {"created": 5, "updated": 0, "skipped": 0, "assigned": 0}

    pastry = db.query(Product).filter_by(product_code="10000014").one()
    assert pastry.display_name == "Pastry Bag, White"  # quoted comma survives
    assert pastry.category == "Cafe Supply"
    assert pastry.unit_of_measure == "box(es)"
    assert pastry.case_quantity == 1

    sugar_raw = db.query(Product).filter_by(product_code="30000018").one()
    assert sugar_raw.category is None  # blank group

    splenda = db.query(Product).filter_by(product_code="20000008").one()
    assert splenda.unit_of_measure == "EA"  # blank unit falls back


def test_import_is_idempotent_and_updates_existing(db):
    import_products_csv(db, SAMPLE)
    # Re-import with a changed description for one code.
    changed = SAMPLE.replace("Receipt rolls", "Receipt Rolls 80mm")
    result = import_products_csv(db, changed)
    assert result["created"] == 0 and result["updated"] == 5
    assert db.query(Product).count() == 5
    assert db.query(Product).filter_by(product_code="10000003").one().display_name == "Receipt Rolls 80mm"


def test_import_assigns_to_all_active_stores(db):
    for code, name in (("KTOWN", "Ktown"), ("GARDENA", "Gardena")):
        db.add(Store(store_code=code, store_name=name, unleashed_customer_code=code, active=True))
    db.commit()

    result = import_products_csv(db, SAMPLE, assign_all_stores=True, default_par=6, default_min=2)
    assert result["assigned"] == 10  # 5 products x 2 stores

    rows = db.query(StoreInventory).all()
    assert len(rows) == 10
    assert all(r.par_level == 6 and r.minimum_level == 2 and r.current_count == 0 for r in rows)
    tags = {r.tag_id for r in rows}
    assert "KTOWN-10000003" in tags and "GARDENA-30000042" in tags

    # Re-import assigns nothing new.
    again = import_products_csv(db, SAMPLE, assign_all_stores=True)
    assert again["assigned"] == 0
    assert db.query(StoreInventory).count() == 10


def test_import_rejects_files_without_header(db):
    with pytest.raises(ImportFormatError):
        import_products_csv(db, "name,qty\nfoo,1\n")
