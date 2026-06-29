"""Employee scan flow — the simplest possible interaction.

A QR code / NFC tag encodes the URL /scan/{tag_id}. The employee:
  1. scans (opens this page),
  2. sees the item name + current count,
  3. taps a quantity (1 / 2 / 3 / Case / Custom),
  4. submits.

Default and only action is STORE_REMOVAL ("Removed from Back Storage for Café
Use"). No login, no store/warehouse/action pickers, no item search.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy.orm import Session

from ..database import get_db
from ..services import inventory_service
from ..templating import render

router = APIRouter()


def _load(db: Session, tag_id: str):
    inv = inventory_service.get_inventory_by_tag(db, tag_id)
    if inv is None:
        return None
    return inv


@router.get("/scan/{tag_id}")
def scan_page(tag_id: str, request: Request, db: Session = Depends(get_db)):
    inv = _load(db, tag_id)
    if inv is None:
        return render(request, "scan_unknown.html", {"tag_id": tag_id})
    return render(
        request,
        "scan.html",
        {"inv": inv, "product": inv.product, "store": inv.store, "tag_id": tag_id},
    )


@router.post("/scan/{tag_id}")
def scan_submit(
    tag_id: str,
    request: Request,
    quantity: float = Form(...),
    db: Session = Depends(get_db),
):
    """No-JS fallback path: posts the form and renders a confirmation page."""
    inv = _load(db, tag_id)
    if inv is None:
        return render(request, "scan_unknown.html", {"tag_id": tag_id})

    employee_id = request.session.get("user_id")
    error = None
    result = None
    if quantity <= 0:
        error = "Quantity must be greater than zero."
    else:
        result = inventory_service.record_removal(
            db, inventory=inv, quantity=quantity, employee_id=employee_id, source="scan"
        )
    db.refresh(inv)
    return render(
        request,
        "scan_result.html",
        {
            "inv": inv,
            "product": inv.product,
            "store": inv.store,
            "tag_id": tag_id,
            "quantity": quantity,
            "result": result,
            "error": error,
        },
    )


@router.post("/api/scan/{tag_id}")
def scan_submit_json(
    tag_id: str,
    request: Request,
    quantity: float = Form(...),
    db: Session = Depends(get_db),
):
    """JSON path used by the page's JS for an instant inline confirmation."""
    inv = _load(db, tag_id)
    if inv is None:
        return {"ok": False, "error": "Unknown tag"}
    if quantity <= 0:
        return {"ok": False, "error": "Quantity must be greater than zero"}

    employee_id = request.session.get("user_id")
    result = inventory_service.record_removal(
        db, inventory=inv, quantity=quantity, employee_id=employee_id, source="scan"
    )
    name = inv.product.display_name
    qty_label = f"{quantity:g}"
    return {
        "ok": True,
        "message": f"Removed {qty_label} {name}. New count: {result.quantity_after:g}.",
        "new_count": result.quantity_after,
        "unit": inv.product.unit_of_measure,
    }
