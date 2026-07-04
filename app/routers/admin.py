"""Admin setup: stores, products, store inventory/par/tags, users, Unleashed settings."""
from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..config import get_settings
from ..database import get_db
from ..enums import Role
from ..integrations.unleashed import UnleashedClient, UnleashedError
from ..models import DailyRequest, DailyRequestLine, InventoryTransaction, Product, Store, StoreInventory, User
from ..security import hash_password, require_roles
from ..templating import render

router = APIRouter(prefix="/admin")

AdminUser = Depends(require_roles(Role.ADMIN))


def _redirect(path: str, *, msg: str | None = None, err: str | None = None) -> RedirectResponse:
    """303 redirect carrying a one-shot flash message as a query param."""
    for key, text in (("msg", msg), ("err", err)):
        if text:
            path += ("&" if "?" in path else "?") + f"{key}={quote(text)}"
    return RedirectResponse(url=path, status_code=303)


@router.get("")
def admin_home(request: Request, db: Session = Depends(get_db), user: User = AdminUser):
    settings = get_settings()
    counts = {
        "stores": db.query(Store).count(),
        "products": db.query(Product).count(),
        "inventory": db.query(StoreInventory).count(),
        "users": db.query(User).count(),
    }
    return render(request, "admin/home.html", {"counts": counts, "unleashed_configured": settings.unleashed_configured})


# ---- Stores ----------------------------------------------------------
@router.get("/stores")
def stores_page(
    request: Request,
    edit: int | None = None,
    confirm_delete: int | None = None,
    msg: str | None = None,
    err: str | None = None,
    db: Session = Depends(get_db),
    user: User = AdminUser,
):
    stores = list(db.execute(select(Store).order_by(Store.store_name)).scalars())
    editing = db.get(Store, edit) if edit else None
    confirming = db.get(Store, confirm_delete) if confirm_delete else None
    counts = None
    if confirming is not None:
        counts = {
            "inventory": db.query(StoreInventory.id).filter(StoreInventory.store_id == confirming.id).count(),
            "requests": db.query(DailyRequest.id).filter(DailyRequest.store_id == confirming.id).count(),
            "transactions": db.query(InventoryTransaction.id)
            .filter(InventoryTransaction.store_id == confirming.id)
            .count(),
        }
    return render(
        request,
        "admin/stores.html",
        {"stores": stores, "editing": editing, "confirming": confirming, "confirm_counts": counts,
         "msg": msg, "err": err},
    )


@router.post("/stores")
def stores_save(
    request: Request,
    id: int | None = Form(None),
    store_code: str = Form(...),
    store_name: str = Form(...),
    unleashed_customer_code: str = Form(...),
    unleashed_customer_guid: str = Form(""),
    active: bool = Form(False),
    db: Session = Depends(get_db),
    user: User = AdminUser,
):
    store = db.get(Store, id) if id else Store()
    store.store_code = store_code.strip()
    store.store_name = store_name.strip()
    store.unleashed_customer_code = unleashed_customer_code.strip()
    store.unleashed_customer_guid = unleashed_customer_guid.strip() or None
    store.active = active
    if not id:
        db.add(store)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return _redirect("/admin/stores", err=f"Store code '{store_code.strip()}' is already in use.")
    return _redirect("/admin/stores", msg=f"Store {store.store_name} saved.")


@router.post("/stores/{store_id}/delete")
def stores_delete(
    store_id: int,
    force: bool = Form(False),
    db: Session = Depends(get_db),
    user: User = AdminUser,
):
    store = db.get(Store, store_id)
    if store is None:
        return _redirect("/admin/stores", err="Store not found.")
    name = store.store_name
    has_history = (
        db.query(StoreInventory.id).filter(StoreInventory.store_id == store_id).first() is not None
        or db.query(InventoryTransaction.id).filter(InventoryTransaction.store_id == store_id).first() is not None
        or db.query(DailyRequest.id).filter(DailyRequest.store_id == store_id).first() is not None
    )
    if has_history and not force:
        # Deleting history is irreversible — show an explicit confirmation first.
        return _redirect(f"/admin/stores?confirm_delete={store_id}")
    if has_history:
        # Purge in FK order: ledger -> request lines -> requests -> inventory.
        request_ids = [rid for (rid,) in db.query(DailyRequest.id).filter(DailyRequest.store_id == store_id)]
        db.query(InventoryTransaction).filter(InventoryTransaction.store_id == store_id).delete(
            synchronize_session=False
        )
        if request_ids:
            db.query(DailyRequestLine).filter(DailyRequestLine.daily_request_id.in_(request_ids)).delete(
                synchronize_session=False
            )
            db.query(DailyRequest).filter(DailyRequest.id.in_(request_ids)).delete(synchronize_session=False)
        db.query(StoreInventory).filter(StoreInventory.store_id == store_id).delete(synchronize_session=False)
    db.query(User).filter(User.store_id == store_id).update({"store_id": None})
    db.delete(store)
    db.commit()
    if has_history:
        return _redirect("/admin/stores", msg=f"Store {name} and all its history permanently deleted.")
    return _redirect("/admin/stores", msg=f"Store {name} deleted.")


# ---- Products --------------------------------------------------------
@router.get("/products")
def products_page(request: Request, db: Session = Depends(get_db), user: User = AdminUser):
    products = list(db.execute(select(Product).order_by(Product.display_name)).scalars())
    return render(request, "admin/products.html", {"products": products})


@router.post("/products")
def products_save(
    request: Request,
    id: int | None = Form(None),
    product_code: str = Form(...),
    display_name: str = Form(...),
    category: str = Form(""),
    unit_of_measure: str = Form("EA"),
    case_quantity: int = Form(1),
    unleashed_product_guid: str = Form(""),
    active: bool = Form(False),
    db: Session = Depends(get_db),
    user: User = AdminUser,
):
    product = db.get(Product, id) if id else Product()
    product.product_code = product_code.strip()
    product.display_name = display_name.strip()
    product.category = category.strip() or None
    product.unit_of_measure = unit_of_measure.strip() or "EA"
    product.case_quantity = case_quantity or 1
    product.unleashed_product_guid = unleashed_product_guid.strip() or None
    product.active = active
    if not id:
        db.add(product)
    db.commit()
    return RedirectResponse(url="/admin/products", status_code=303)


# ---- Store inventory / par / tags ------------------------------------
@router.get("/inventory")
def inventory_page(request: Request, store_id: int | None = None, db: Session = Depends(get_db), user: User = AdminUser):
    stores = list(db.execute(select(Store).order_by(Store.store_name)).scalars())
    sid = store_id or (stores[0].id if stores else None)
    rows = []
    if sid:
        rows = db.execute(
            select(StoreInventory, Product)
            .join(Product, Product.id == StoreInventory.product_id)
            .where(StoreInventory.store_id == sid)
            .order_by(Product.display_name)
        ).all()
    products = list(db.execute(select(Product).where(Product.active.is_(True)).order_by(Product.display_name)).scalars())
    return render(
        request,
        "admin/inventory.html",
        {"stores": stores, "store_id": sid, "rows": rows, "products": products, "base_url": get_settings().base_url},
    )


@router.post("/inventory")
def inventory_save(
    request: Request,
    id: int | None = Form(None),
    store_id: int = Form(...),
    product_id: int = Form(...),
    par_level: float = Form(0),
    minimum_level: float = Form(0),
    current_count: float = Form(0),
    storage_location: str = Form(""),
    tag_id: str = Form(""),
    active: bool = Form(False),
    db: Session = Depends(get_db),
    user: User = AdminUser,
):
    inv = db.get(StoreInventory, id) if id else StoreInventory(store_id=store_id, product_id=product_id)
    inv.store_id = store_id
    inv.product_id = product_id
    inv.par_level = par_level
    inv.minimum_level = minimum_level
    # current_count is set here only at initial setup; thereafter it changes only
    # via transaction-safe service functions (removals/receipts/adjustments).
    if not id:
        inv.current_count = current_count
    inv.storage_location = storage_location.strip() or None
    inv.tag_id = tag_id.strip() or None
    inv.active = active
    if not id:
        db.add(inv)
    db.commit()
    return RedirectResponse(url=f"/admin/inventory?store_id={store_id}", status_code=303)


@router.get("/tags")
def tags_page(request: Request, db: Session = Depends(get_db), user: User = AdminUser):
    rows = db.execute(
        select(StoreInventory, Product, Store)
        .join(Product, Product.id == StoreInventory.product_id)
        .join(Store, Store.id == StoreInventory.store_id)
        .where(StoreInventory.tag_id.is_not(None))
        .order_by(Store.store_name, Product.display_name)
    ).all()
    return render(request, "admin/tags.html", {"rows": rows, "base_url": get_settings().base_url})


# ---- Users -----------------------------------------------------------
@router.get("/users")
def users_page(
    request: Request,
    edit: int | None = None,
    confirm_delete: int | None = None,
    msg: str | None = None,
    err: str | None = None,
    db: Session = Depends(get_db),
    user: User = AdminUser,
):
    users = list(db.execute(select(User).order_by(User.name)).scalars())
    stores = list(db.execute(select(Store).order_by(Store.store_name)).scalars())
    editing = db.get(User, edit) if edit else None
    confirming = db.get(User, confirm_delete) if confirm_delete else None
    return render(
        request,
        "admin/users.html",
        {"users": users, "stores": stores, "roles": sorted(Role.ALL), "editing": editing,
         "confirming": confirming, "msg": msg, "err": err},
    )


@router.post("/users")
def users_save(
    request: Request,
    id: int | None = Form(None),
    name: str = Form(...),
    email: str = Form(...),
    role: str = Form(Role.EMPLOYEE),
    store_id: int | None = Form(None),
    password: str = Form(""),
    active: bool = Form(False),
    db: Session = Depends(get_db),
    user: User = AdminUser,
):
    target = db.get(User, id) if id else User(email=email.strip().lower())
    # Guard against locking yourself out by demoting/deactivating the last admin.
    if id and target is not None and target.role == Role.ADMIN and (role != Role.ADMIN or not active):
        other_admins = (
            db.query(User.id)
            .filter(User.role == Role.ADMIN, User.active.is_(True), User.id != target.id)
            .first()
        )
        if other_admins is None:
            return _redirect("/admin/users", err="Cannot demote or deactivate the last active admin.")
    target.name = name.strip()
    target.email = email.strip().lower()
    target.role = role if role in Role.ALL else Role.EMPLOYEE
    target.store_id = store_id or None
    target.active = active
    if password:
        target.password_hash = hash_password(password)
    if not id:
        db.add(target)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return _redirect("/admin/users", err=f"Email '{email.strip().lower()}' is already in use.")
    return _redirect("/admin/users", msg=f"User {target.name} saved.")


@router.post("/users/{user_id}/delete")
def users_delete(
    user_id: int,
    force: bool = Form(False),
    db: Session = Depends(get_db),
    user: User = AdminUser,
):
    target = db.get(User, user_id)
    if target is None:
        return _redirect("/admin/users", err="User not found.")
    if target.id == user.id:
        return _redirect("/admin/users", err="You can't delete your own account.")
    if target.role == Role.ADMIN and target.active:
        other_admins = (
            db.query(User.id)
            .filter(User.role == Role.ADMIN, User.active.is_(True), User.id != target.id)
            .first()
        )
        if other_admins is None:
            return _redirect("/admin/users", err="Cannot delete the last active admin.")
    name = target.name
    has_history = (
        db.query(InventoryTransaction.id).filter(InventoryTransaction.employee_id == user_id).first() is not None
        or db.query(DailyRequest.id).filter(DailyRequest.submitted_by == user_id).first() is not None
    )
    if has_history and not force:
        return _redirect(f"/admin/users?confirm_delete={user_id}")
    if has_history:
        # Keep the store's history; just remove this person's name from it.
        db.query(InventoryTransaction).filter(InventoryTransaction.employee_id == user_id).update(
            {"employee_id": None}, synchronize_session=False
        )
        db.query(DailyRequest).filter(DailyRequest.submitted_by == user_id).update(
            {"submitted_by": None}, synchronize_session=False
        )
    db.delete(target)
    db.commit()
    return _redirect("/admin/users", msg=f"User {name} deleted.")


# ---- Unleashed settings ---------------------------------------------
@router.get("/settings")
def settings_page(request: Request, db: Session = Depends(get_db), user: User = AdminUser):
    s = get_settings()
    view = {
        "configured": s.unleashed_configured,
        "api_url": s.unleashed_api_url,
        "fulfill_warehouse_code": s.unleashed_fulfill_warehouse_code,
        "create_order_status": s.unleashed_create_order_status,
        "use_shipments": s.unleashed_receipt_use_shipments,
        "fallback_to_order": s.unleashed_receipt_fallback_to_order,
        "polling_enabled": s.polling_enabled,
        "polling_interval_minutes": s.polling_interval_minutes,
        # Secrets are NEVER rendered — only whether they are present.
        "api_id_set": bool(s.unleashed_api_id),
        "api_key_set": bool(s.unleashed_api_key),
    }
    return render(request, "admin/settings.html", {"v": view, "test_result": None})


@router.post("/settings/test")
def settings_test(request: Request, db: Session = Depends(get_db), user: User = AdminUser):
    s = get_settings()
    test_result = {"ok": False, "message": ""}
    if not s.unleashed_configured:
        test_result["message"] = "Credentials not configured (set UNLEASHED_API_ID / UNLEASHED_API_KEY)."
    else:
        try:
            UnleashedClient().ping()
            test_result = {"ok": True, "message": "Connected to Unleashed successfully."}
        except UnleashedError as exc:
            test_result["message"] = f"Connection failed: {exc}"
    view = {
        "configured": s.unleashed_configured,
        "api_url": s.unleashed_api_url,
        "fulfill_warehouse_code": s.unleashed_fulfill_warehouse_code,
        "create_order_status": s.unleashed_create_order_status,
        "use_shipments": s.unleashed_receipt_use_shipments,
        "fallback_to_order": s.unleashed_receipt_fallback_to_order,
        "polling_enabled": s.polling_enabled,
        "polling_interval_minutes": s.polling_interval_minutes,
        "api_id_set": bool(s.unleashed_api_id),
        "api_key_set": bool(s.unleashed_api_key),
    }
    return render(request, "admin/settings.html", {"v": view, "test_result": test_result})
