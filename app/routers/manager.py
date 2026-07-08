"""Store Manager screens: live inventory, count corrections, daily requests."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..database import get_db
from ..enums import Role
from ..integrations.unleashed import UnleashedError
from ..models import DailyRequest, InventoryTransaction, Product, Store, StoreInventory, User
from ..security import require_roles
from ..services import attention_service, inventory_service, request_service, settings_service
from ..templating import render

router = APIRouter(prefix="/manager")

ManagerUser = Depends(require_roles(Role.STORE_MANAGER, Role.ADMIN))


def _stores(db: Session) -> list[Store]:
    return list(db.execute(select(Store).where(Store.active.is_(True)).order_by(Store.store_name)).scalars())


def _resolve_store_id(user: User, store_id: int | None, db: Session) -> int | None:
    if store_id:
        return store_id
    if user.store_id:
        return user.store_id
    stores = _stores(db)
    return stores[0].id if stores else None


@router.get("/inventory")
def inventory_page(
    request: Request, store_id: int | None = None, db: Session = Depends(get_db), user: User = ManagerUser
):
    sid = _resolve_store_id(user, store_id, db)
    rows = []
    if sid:
        rows = db.execute(
            select(StoreInventory, Product)
            .join(Product, Product.id == StoreInventory.product_id)
            .where(StoreInventory.store_id == sid, StoreInventory.active.is_(True))
            .order_by(Product.display_name)
        ).all()
    items = [
        {
            "inv": inv,
            "product": product,
            "suggested": max(inv.par_level - inv.current_count, 0),
            "below_par": inv.current_count < inv.par_level,
            "below_min": inv.current_count < inv.minimum_level,
        }
        for inv, product in rows
    ]
    return render(
        request,
        "manager/inventory.html",
        {"stores": _stores(db), "store_id": sid, "items": items},
    )


@router.post("/inventory/adjust")
def adjust_count(
    request: Request,
    inventory_id: int = Form(...),
    new_count: float = Form(...),
    note: str = Form(""),
    store_id: int | None = Form(None),
    db: Session = Depends(get_db),
    user: User = ManagerUser,
):
    inv = db.get(StoreInventory, inventory_id)
    if inv is not None:
        inventory_service.record_count_adjustment(
            db, inventory=inv, new_count=new_count, employee_id=user.id, note=note or None
        )
    return RedirectResponse(url=f"/manager/inventory?store_id={store_id or inv.store_id}", status_code=303)


@router.get("/history")
def history_page(
    request: Request,
    store_id: int | None = None,
    product_id: int | None = None,
    db: Session = Depends(get_db),
    user: User = ManagerUser,
):
    """The inventory ledger: every count change with its cause (scan, order, correction)."""
    sid = _resolve_store_id(user, store_id, db)
    rows = []
    products = []
    if sid:
        q = (
            select(InventoryTransaction, Product, User)
            .join(Product, Product.id == InventoryTransaction.product_id)
            .outerjoin(User, User.id == InventoryTransaction.employee_id)
            .where(InventoryTransaction.store_id == sid)
            .order_by(InventoryTransaction.id.desc())
            .limit(200)
        )
        if product_id:
            q = q.where(InventoryTransaction.product_id == product_id)
        rows = db.execute(q).all()
        products = list(
            db.execute(select(Product).where(Product.active.is_(True)).order_by(Product.display_name)).scalars()
        )
    return render(
        request,
        "manager/history.html",
        {"stores": _stores(db), "store_id": sid, "product_id": product_id, "rows": rows, "products": products},
    )


@router.get("/requests")
def requests_list(
    request: Request, store_id: int | None = None, db: Session = Depends(get_db), user: User = ManagerUser
):
    sid = _resolve_store_id(user, store_id, db)
    reqs = []
    if sid:
        reqs = list(
            db.execute(
                select(DailyRequest)
                .where(DailyRequest.store_id == sid)
                .order_by(DailyRequest.request_date.desc(), DailyRequest.id.desc())
            ).scalars()
        )
    return render(
        request,
        "manager/requests.html",
        {"stores": _stores(db), "store_id": sid, "requests": reqs,
         "attention": attention_service.get_attention(db, store_id=sid), "attention_links": True},
    )


@router.post("/requests/generate")
def generate_request(
    request: Request,
    store_id: int = Form(...),
    db: Session = Depends(get_db),
    user: User = ManagerUser,
):
    # Business-timezone "today" (the server runs UTC; late afternoon local time
    # is already tomorrow in UTC) — keeps manual and auto-submitted requests on
    # the same daily request.
    req = request_service.generate_daily_request(
        db, store_id=store_id, request_date=settings_service.local_today(db)
    )
    return RedirectResponse(url=f"/manager/requests/{req.id}", status_code=303)


@router.get("/requests/{request_id}")
def request_detail(
    request_id: int, request: Request, db: Session = Depends(get_db), user: User = ManagerUser
):
    req = db.get(DailyRequest, request_id)
    if req is None:
        return RedirectResponse(url="/manager/requests", status_code=303)
    lines = [(ln, db.get(Product, ln.product_id)) for ln in sorted(req.lines, key=lambda x: x.sales_order_line_number or 0)]
    return render(
        request,
        "manager/request_detail.html",
        {"req": req, "store": db.get(Store, req.store_id), "lines": lines},
    )


@router.post("/requests/{request_id}/line/{line_id}")
def override_line(
    request_id: int,
    line_id: int,
    request: Request,
    final_quantity: float = Form(...),
    note: str = Form(""),
    db: Session = Depends(get_db),
    user: User = ManagerUser,
):
    try:
        request_service.override_line(db, line_id=line_id, final_quantity=final_quantity, note=note or None)
    except request_service.RequestError:
        pass
    return RedirectResponse(url=f"/manager/requests/{request_id}", status_code=303)


@router.post("/requests/{request_id}/cancel")
def cancel_request(
    request_id: int, request: Request, db: Session = Depends(get_db), user: User = ManagerUser
):
    try:
        request_service.cancel_request(db, request_id=request_id, cancelled_by=user.id)
    except request_service.RequestError:
        pass
    return RedirectResponse(url=f"/manager/requests/{request_id}", status_code=303)


@router.post("/requests/{request_id}/submit")
def submit_request(
    request_id: int, request: Request, db: Session = Depends(get_db), user: User = ManagerUser
):
    error = None
    try:
        request_service.submit_to_unleashed(db, request_id=request_id, submitted_by=user.id)
    except (UnleashedError, request_service.RequestError) as exc:
        error = str(exc)
    req = db.get(DailyRequest, request_id)
    lines = [(ln, db.get(Product, ln.product_id)) for ln in sorted(req.lines, key=lambda x: x.sales_order_line_number or 0)]
    return render(
        request,
        "manager/request_detail.html",
        {"req": req, "store": db.get(Store, req.store_id), "lines": lines, "error": error},
    )
