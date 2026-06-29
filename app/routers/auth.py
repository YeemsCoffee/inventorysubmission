"""Login / logout for manager, warehouse and admin users.

The employee scan flow is intentionally NOT behind this login (see scan.py).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import User
from ..security import verify_password
from ..templating import render

router = APIRouter()


@router.get("/login")
def login_form(request: Request, next: str = "/"):
    return render(request, "login.html", {"next": next, "error": None})


@router.post("/login")
def login_submit(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    next: str = Form("/"),
    db: Session = Depends(get_db),
):
    user = db.execute(select(User).where(User.email == email.strip().lower())).scalar_one_or_none()
    if user is None or not user.active or not verify_password(password, user.password_hash):
        return render(request, "login.html", {"next": next, "error": "Invalid email or password"})
    request.session["user_id"] = user.id
    request.session["user_name"] = user.name
    request.session["user_role"] = user.role
    request.session["user_store_id"] = user.store_id
    return RedirectResponse(url=next or "/", status_code=303)


@router.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/login", status_code=303)
