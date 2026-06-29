"""Unleashed webhook receiver.

Register this endpoint as the destination of a Sales Order webhook subscription
in Unleashed. Calls must present the shared secret (header `X-Webhook-Secret` or
`?secret=`). We always store the event and dedupe on the notification id, then
run the idempotent receipt path. Returns 200 quickly so Unleashed does not retry
for issues we have already recorded.
"""
from __future__ import annotations

import hmac
import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from ..config import get_settings
from ..database import get_db
from ..services import webhook_service

logger = logging.getLogger("webhooks")
router = APIRouter(prefix="/webhooks")


def _authorized(request: Request, settings) -> bool:
    presented = request.headers.get("x-webhook-secret") or request.query_params.get("secret") or ""
    return hmac.compare_digest(presented, settings.unleashed_webhook_secret)


@router.post("/unleashed")
async def unleashed_webhook(request: Request, db: Session = Depends(get_db)):
    settings = get_settings()
    if not _authorized(request, settings):
        return JSONResponse({"status": "unauthorized"}, status_code=401)

    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse({"status": "bad_payload"}, status_code=400)
    if not isinstance(payload, dict):
        return JSONResponse({"status": "bad_payload"}, status_code=400)

    result = webhook_service.handle_webhook(db, payload)
    return JSONResponse({"status": "ok", "result": result}, status_code=200)
