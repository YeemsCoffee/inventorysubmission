"""Application configuration.

All settings (including the Unleashed credentials) are read from environment
variables / a local `.env` file. Credentials therefore live ONLY on the backend
and are never compiled into any frontend asset.
"""
from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- App ---
    app_env: str = "local"
    secret_key: str = "dev-insecure-secret-change-me"
    base_url: str = "http://localhost:8000"

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

    @property
    def unleashed_configured(self) -> bool:
        return bool(self.unleashed_api_id and self.unleashed_api_key)

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")


@lru_cache
def get_settings() -> Settings:
    return Settings()
