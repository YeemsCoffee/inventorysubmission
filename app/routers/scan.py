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

import hashlib
import hmac

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy.orm import Session

from ..config import get_settings
from ..database import get_db
from ..services import inventory_service
from ..templating import render

router = APIRouter()


def _load(db: Session, tag_id: str):
    inv = inventory_service.get_inventory_by_tag(db, tag_id)
    if inv is None:
        return None
    return inv


def _undo_token(transaction_id: int) -> str:
    """Signed token proving the caller made this removal (the scan flow has no
    login, and ledger ids are guessable — the token is not)."""
    secret = get_settings().secret_key.encode("utf-8")
    return hmac.new(secret, f"undo:{transaction_id}".encode("utf-8"), hashlib.sha256).hexdigest()[:24]


def _undo(db: Session, inv, transaction_id: int, token: str):
    """Shared guard + service call for both undo endpoints. Returns (result, error)."""
    if not hmac.compare_digest(token, _undo_token(transaction_id)):
        return None, "This undo link is not valid."
    try:
        return inventory_service.undo_removal(db, transaction_id=transaction_id, inventory=inv), None
    except inventory_service.InventoryError as exc:
        return None, str(exc)


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
    undo_token = _undo_token(result.transaction.id) if result and result.transaction else None
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
            "undo_token": undo_token,
            "undone": False,
        },
    )


@router.post("/scan/{tag_id}/undo")
def scan_undo(
    tag_id: str,
    request: Request,
    txn_id: int = Form(...),
    token: str = Form(...),
    db: Session = Depends(get_db),
):
    """No-JS fallback: undo the removal and show a confirmation page."""
    inv = _load(db, tag_id)
    if inv is None:
        return render(request, "scan_unknown.html", {"tag_id": tag_id})
    result, error = _undo(db, inv, txn_id, token)
    db.refresh(inv)
    return render(
        request,
        "scan_result.html",
        {
            "inv": inv,
            "product": inv.product,
            "store": inv.store,
            "tag_id": tag_id,
            "quantity": 0,
            "result": result,
            "error": error,
            "undo_token": None,
            "undone": result is not None,
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
        "txn_id": result.transaction.id if result.transaction else None,
        "undo_token": _undo_token(result.transaction.id) if result.transaction else None,
    }


@router.post("/api/scan/{tag_id}/undo")
def scan_undo_json(
    tag_id: str,
    request: Request,
    txn_id: int = Form(...),
    token: str = Form(...),
    db: Session = Depends(get_db),
):
    """JSON undo used by the page's JS."""
    inv = _load(db, tag_id)
    if inv is None:
        return {"ok": False, "error": "Unknown tag"}
    result, error = _undo(db, inv, txn_id, token)
    if error:
        return {"ok": False, "error": error}
    return {
        "ok": True,
        "message": f"Undone. Count back to {result.quantity_after:g}.",
        "new_count": result.quantity_after,
    }
