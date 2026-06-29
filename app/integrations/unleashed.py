"""Unleashed Software API client.

Auth (per https://apidocs.unleashedsoftware.com/AuthenticationHelp):
  - The signature is an HMAC-SHA256 of the *query string only* (the part after
    `?`, excluding the `?` and the endpoint name), using the API KEY as the
    secret, then Base64-encoded.
  - For requests with no query string, sign the empty string "".
  - Send headers: api-auth-id, api-auth-signature, Accept/Content-Type JSON.
  - A bad signature yields HTTP 403.

Endpoints used:
  - POST /SalesOrders/{guid}        create a replenishment order (idempotent on Guid)
  - GET  /SalesOrders/{guid}        re-read an order by Guid
  - GET  /SalesOrders/{page}?...    list/filter orders (polling)
  - GET  /SalesShipments?...        actual shipped quantities for receipts
  - GET  /Customers?...             admin lookups (store -> customer mapping)
  - GET  /Products?...              admin lookups (product code/guid)

Credentials come from env only and are never logged.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
from typing import Any
from urllib.parse import urlencode

import httpx

from ..config import Settings, get_settings

logger = logging.getLogger("unleashed")


class UnleashedError(Exception):
    """Raised on transport/HTTP errors. Message is safe to log (no secrets)."""

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


def compute_signature(query_string: str, api_key: str) -> str:
    """HMAC-SHA256(query_string, key=api_key) -> base64. Empty string is valid."""
    digest = hmac.new(
        api_key.encode("utf-8"), query_string.encode("utf-8"), hashlib.sha256
    ).digest()
    return base64.b64encode(digest).decode("utf-8")


class UnleashedClient:
    def __init__(self, settings: Settings | None = None, timeout: float = 30.0):
        self.settings = settings or get_settings()
        self.base_url = self.settings.unleashed_api_url.rstrip("/")
        self.api_id = self.settings.unleashed_api_id
        self.api_key = self.settings.unleashed_api_key
        self.timeout = timeout

    # -- low level -------------------------------------------------------
    def _headers(self, query_string: str) -> dict[str, str]:
        return {
            "api-auth-id": self.api_id,
            "api-auth-signature": compute_signature(query_string, self.api_key),
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Client-Type": "cafe-inventory-mvp",
        }

    def _request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not self.settings.unleashed_configured:
            raise UnleashedError("Unleashed API credentials are not configured")

        # Build the query string ONCE and use the identical string for both the
        # signature and the URL, so they can never diverge.
        query_string = urlencode(params, doseq=True) if params else ""
        url = f"{self.base_url}{path}"
        if query_string:
            url = f"{url}?{query_string}"

        try:
            resp = httpx.request(
                method,
                url,
                headers=self._headers(query_string),
                json=json_body,
                timeout=self.timeout,
            )
        except httpx.HTTPError as exc:  # network/timeout
            raise UnleashedError(f"Unleashed request failed: {exc.__class__.__name__}") from exc

        if resp.status_code >= 400:
            # Never echo headers/secrets; include a trimmed body for diagnostics.
            body = (resp.text or "")[:500]
            raise UnleashedError(
                f"Unleashed {method} {path} -> {resp.status_code}: {body}",
                status_code=resp.status_code,
            )

        if not resp.content:
            return {}
        try:
            return resp.json()
        except ValueError:
            return {"_raw": resp.text}

    # -- sales orders ----------------------------------------------------
    def create_sales_order(self, guid: str, payload: dict[str, Any]) -> dict[str, Any]:
        """POST a sales order to /SalesOrders/{guid}. Idempotent on Guid:
        re-posting the same Guid updates that order instead of duplicating it."""
        return self._request("POST", f"/SalesOrders/{guid}", json_body=payload)

    def get_sales_order(self, guid: str) -> dict[str, Any]:
        return self._request("GET", f"/SalesOrders/{guid}")

    def list_sales_orders(
        self, page: int = 1, page_size: int = 200, **filters: Any
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"pageSize": page_size, **filters}
        return self._request("GET", f"/SalesOrders/{page}", params=params)

    # -- shipments -------------------------------------------------------
    def get_shipments_for_order(self, order_number: str) -> list[dict[str, Any]]:
        """Return shipment records for a given sales order number."""
        data = self._request("GET", "/SalesShipments", params={"orderNumber": order_number})
        return data.get("Items", []) if isinstance(data, dict) else []

    # -- reference data (admin) -----------------------------------------
    def get_customers(self, **filters: Any) -> list[dict[str, Any]]:
        data = self._request("GET", "/Customers", params=filters or None)
        return data.get("Items", []) if isinstance(data, dict) else []

    def get_products(self, **filters: Any) -> list[dict[str, Any]]:
        data = self._request("GET", "/Products", params=filters or None)
        return data.get("Items", []) if isinstance(data, dict) else []

    def ping(self) -> bool:
        """Cheap connectivity/auth check used by the admin settings page."""
        self._request("GET", "/SalesOrders/1", params={"pageSize": 1})
        return True
