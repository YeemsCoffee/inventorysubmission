"""Shared Jinja2 templates instance + small helpers used by routers."""
from __future__ import annotations

from pathlib import Path

from fastapi import Request
from fastapi.templating import Jinja2Templates

from .config import get_settings

TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def _fmt_qty(value) -> str:
    """Render numbers without trailing .0 (12.0 -> '12', 12.5 -> '12.5')."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return str(value)
    return f"{f:g}"


templates.env.filters["qty"] = _fmt_qty


def session_user(request: Request) -> dict | None:
    """Lightweight identity from the signed session cookie (no DB hit)."""
    uid = request.session.get("user_id")
    if not uid:
        return None
    return {
        "id": uid,
        "name": request.session.get("user_name"),
        "role": request.session.get("user_role"),
        "store_id": request.session.get("user_store_id"),
    }


def _attention_for(user: dict | None) -> dict | None:
    """Needs-attention items for staff pages, rendered globally via base.html.

    One bounded, indexed query per authenticated page view; managers see their
    own store's problems, warehouse/admin see everything. Never breaks a page:
    any failure degrades to no banner.
    """
    if user is None or user.get("role") == "EMPLOYEE":
        return None
    try:
        from .database import SessionLocal
        from .services import attention_service

        db = SessionLocal()
        try:
            store_id = user.get("store_id") if user.get("role") == "STORE_MANAGER" else None
            return attention_service.get_attention(db, store_id=store_id)
        finally:
            db.close()
    except Exception:  # noqa: BLE001 - the banner must never take a page down
        return None


def render(request: Request, name: str, context: dict | None = None):
    """Render a template with common context (request, user, settings) merged in."""
    user = session_user(request)
    ctx = {
        "request": request,
        "user": user,
        "settings": get_settings(),
        "attention": _attention_for(user),
    }
    if context:
        ctx.update(context)
    return templates.TemplateResponse(name, ctx)
