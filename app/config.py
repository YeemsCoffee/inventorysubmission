"""Application configuration.

All settings (including the Unleashed credentials) are read from environment
variables / a local `.env` file. Credentials therefore live ONLY on the backend
and are never compiled into any frontend asset.
"""
from __future__ import annotations

from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- App ---
    app_env: str = "local"
    secret_key: str = "dev-insecure-secret-change-me"
    base_url: str = "http://localhost:8000"
    # One-time bootstrap: seed stores/products/users on boot (idempotent).
    # Set SEED_ON_STARTUP=true for the first deploy, then remove it.
    seed_on_startup: bool = False

    # --- Database ---
    database_url: str = "sqlite:///./cafe_inventory.db"

    # --- Unleashed API (backend only) ---
    unleashed_api_id: str = ""
    unleashed_api_key: str = ""
    unleashed_api_url: str = "https://api.unleashedsoftware.com"
    unleashed_fulfill_warehouse_code: str = "MAIN"
    unleashed_create_order_status: str = "Parked"
    unleashed_default_currency: str = ""
    unleashed_default_tax_code: str = ""
    unleashed_receipt_use_shipments: bool = True
    unleashed_receipt_fallback_to_order: bool = True

    # --- Webhooks ---
    unleashed_webhook_secret: str = "change-me-webhook-secret"

    # --- Polling ---
    polling_enabled: bool = True
    polling_interval_minutes: int = 10

    @field_validator("base_url", "unleashed_api_url")
    @classmethod
    def _normalize_url(cls, v: str) -> str:
        # Without a scheme the browser treats built links as *relative* paths
        # (".../admin/<host>/scan/x"); with a trailing slash they get a double
        # slash ("//scan/x"). Both 404 — normalize away the two typos.
        v = v.strip().rstrip("/")
        if v and not v.lower().startswith(("http://", "https://")):
            v = f"https://{v}"
        return v

    @property
    def unleashed_configured(self) -> bool:
        return bool(self.unleashed_api_id and self.unleashed_api_key)

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")


@lru_cache
def get_settings() -> Settings:
    return Settings()
