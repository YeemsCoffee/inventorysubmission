"""Warehouse / Admin: submitted requests + Unleashed sync status, plus retries."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..database import get_db
from ..enums import Role
from ..integrations.unleashed import UnleashedError
from ..models import DailyRequest, Store, User
from ..security import require_roles
from ..services import receipt_service, request_service, sync_service
from ..templating import render

router = APIRouter(prefix="/warehouse")

WarehouseUser = Depends(require_roles(Role.WAREHOUSE, Role.ADMIN))


@router.get("/requests")
def requests_status(request: Request, db: Session = Depends(get_db), user: User = WarehouseUser):
    reqs = list(
        db.execute(select(DailyRequest).order_by(DailyRequest.id.desc())).scalars()
    )
    stores = {s.id: s for s in db.execute(select(Store)).scalars()}
    rows = [{"req": r, "store": stores.get(r.store_id)} for r in reqs]
    return render(request, "warehouse/requests.html", {"rows": rows})


@router.post("/requests/{request_id}/retry-submit")
def retry_submit(request_id: int, request: Request, db: Session = Depends(get_db), user: User = WarehouseUser):
    try:
        request_service.submit_to_unleashed(db, request_id=request_id, submitted_by=user.id)
    except (UnleashedError, request_service.RequestError):
        pass  # status/error_message already persisted by the service
    return RedirectResponse(url="/warehouse/requests", status_code=303)


@router.post("/requests/{request_id}/process-receipt")
def process_receipt(request_id: int, request: Request, db: Session = Depends(get_db), user: User = WarehouseUser):
    receipt_service.process_completion(db, request_id=request_id, source="manual")
    return RedirectResponse(url="/warehouse/requests", status_code=303)


@router.post("/poll-now")
def poll_now(request: Request, db: Session = Depends(get_db), user: User = WarehouseUser):
    sync_service.poll_open_requests(db=db)
    return RedirectResponse(url="/warehouse/requests", status_code=303)
