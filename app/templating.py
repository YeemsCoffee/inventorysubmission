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


def render(request: Request, name: str, context: dict | None = None):
    """Render a template with common context (request, user, settings) merged in."""
    ctx = {
        "request": request,
        "user": session_user(request),
        "settings": get_settings(),
    }
    if context:
        ctx.update(context)
    return templates.TemplateResponse(name, ctx)
