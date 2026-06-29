"""FastAPI application entry point."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from .config import get_settings
from .database import init_db
from .enums import Role
from .routers import admin, auth, manager, scan, warehouse, webhooks
from .scheduler import shutdown_scheduler, start_scheduler
from .templating import render, session_user

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    start_scheduler()
    yield
    shutdown_scheduler()


app = FastAPI(title="Café Inventory & Replenishment", lifespan=lifespan)
app.add_middleware(SessionMiddleware, secret_key=settings.secret_key, max_age=60 * 60 * 12)

STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

app.include_router(scan.router)        # employee (no login)
app.include_router(auth.router)        # login/logout
app.include_router(manager.router)     # store manager
app.include_router(warehouse.router)   # warehouse/admin
app.include_router(admin.router)       # admin
app.include_router(webhooks.router)    # unleashed webhook receiver


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.get("/")
def home(request: Request):
    user = session_user(request)
    if user is None:
        return RedirectResponse(url="/login", status_code=303)
    role = user["role"]
    if role == Role.ADMIN:
        return RedirectResponse(url="/admin", status_code=303)
    if role == Role.WAREHOUSE:
        return RedirectResponse(url="/warehouse/requests", status_code=303)
    if role == Role.STORE_MANAGER:
        return RedirectResponse(url="/manager/inventory", status_code=303)
    # Employees have no dashboard — they only ever use scan links.
    return render(request, "employee_home.html", {})
