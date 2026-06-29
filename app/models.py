"""SQLAlchemy models — the app's source of truth for store-level inventory.

Unleashed remains the source of truth for warehouse stock, sales orders and
fulfillment. These tables only model the store layer plus the mapping/links
back to Unleashed.
"""
from __future__ import annotations

from datetime import datetime, date

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base
from .enums import LineStatus, RequestStatus, Role, WebhookStatus


def _now() -> datetime:
    return datetime.utcnow()


class Store(Base):
    __tablename__ = "stores"

    id: Mapped[int] = mapped_column(primary_key=True)
    store_code: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    store_name: Mapped[str] = mapped_column(String(128))
    # Stores map to Unleashed *customers*, never warehouses.
    unleashed_customer_code: Mapped[str] = mapped_column(String(64))
    unleashed_customer_guid: Mapped[str | None] = mapped_column(String(64), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)

    inventory: Mapped[list["StoreInventory"]] = relationship(back_populates="store")


class Product(Base):
    __tablename__ = "products"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Must match the Unleashed product code.
    product_code: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    unleashed_product_guid: Mapped[str | None] = mapped_column(String(64), nullable=True)
    display_name: Mapped[str] = mapped_column(String(128))
    category: Mapped[str | None] = mapped_column(String(64), nullable=True)
    unit_of_measure: Mapped[str] = mapped_column(String(32), default="EA")
    case_quantity: Mapped[int] = mapped_column(Integer, default=1)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


class StoreInventory(Base):
    """Per-store, per-product local count + par. The authoritative store stock."""

    __tablename__ = "store_inventory"
    __table_args__ = (
        UniqueConstraint("store_id", "product_id", name="uq_store_product"),
        # tag_id maps a scanned QR/NFC tag to exactly one store+product row.
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    store_id: Mapped[int] = mapped_column(ForeignKey("stores.id"), index=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), index=True)
    current_count: Mapped[float] = mapped_column(Float, default=0)
    par_level: Mapped[float] = mapped_column(Float, default=0)
    minimum_level: Mapped[float] = mapped_column(Float, default=0)
    storage_location: Mapped[str | None] = mapped_column(String(64), nullable=True)
    tag_id: Mapped[str | None] = mapped_column(String(64), unique=True, index=True, nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)

    store: Mapped["Store"] = relationship(back_populates="inventory")
    product: Mapped["Product"] = relationship()


class InventoryTransaction(Base):
    """Append-only ledger. Every change to current_count writes one row.

    `idempotency_key` is UNIQUE (nullable) so receipt lines can never be applied
    twice, even under duplicate webhooks/polls/retries.
    """

    __tablename__ = "inventory_transactions"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_txn_idempotency_key"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    store_id: Mapped[int] = mapped_column(ForeignKey("stores.id"), index=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), index=True)
    transaction_type: Mapped[str] = mapped_column(String(40), index=True)
    quantity_delta: Mapped[float] = mapped_column(Float, default=0)
    quantity_before: Mapped[float] = mapped_column(Float, default=0)
    quantity_after: Mapped[float] = mapped_column(Float, default=0)
    source: Mapped[str | None] = mapped_column(String(40), nullable=True)  # scan | manager | webhook | poll | admin
    employee_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    daily_request_id: Mapped[int | None] = mapped_column(ForeignKey("daily_requests.id"), nullable=True)
    daily_request_line_id: Mapped[int | None] = mapped_column(ForeignKey("daily_request_lines.id"), nullable=True)
    unleashed_sales_order_guid: Mapped[str | None] = mapped_column(String(64), nullable=True)
    unleashed_order_number: Mapped[str | None] = mapped_column(String(64), nullable=True)
    unleashed_shipment_guid: Mapped[str | None] = mapped_column(String(64), nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(200), nullable=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=_now, index=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)

    product: Mapped["Product"] = relationship()
    store: Mapped["Store"] = relationship()


class DailyRequest(Base):
    __tablename__ = "daily_requests"

    id: Mapped[int] = mapped_column(primary_key=True)
    store_id: Mapped[int] = mapped_column(ForeignKey("stores.id"), index=True)
    request_date: Mapped[date] = mapped_column(Date, default=date.today, index=True)
    status: Mapped[str] = mapped_column(String(32), default=RequestStatus.DRAFT, index=True)
    generated_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    submitted_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)

    # Unleashed linkage. The Guid is generated locally BEFORE submission so that
    # retries are idempotent (re-POSTing the same Guid updates, never duplicates).
    unleashed_sales_order_guid: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    unleashed_order_number: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    unleashed_source_id: Mapped[str | None] = mapped_column(String(80), unique=True, nullable=True)

    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    received_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)

    store: Mapped["Store"] = relationship()
    lines: Mapped[list["DailyRequestLine"]] = relationship(
        back_populates="request", cascade="all, delete-orphan"
    )


class DailyRequestLine(Base):
    __tablename__ = "daily_request_lines"

    id: Mapped[int] = mapped_column(primary_key=True)
    daily_request_id: Mapped[int] = mapped_column(ForeignKey("daily_requests.id"), index=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), index=True)
    sales_order_line_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    current_count_at_generation: Mapped[float] = mapped_column(Float, default=0)
    par_level: Mapped[float] = mapped_column(Float, default=0)
    suggested_quantity: Mapped[float] = mapped_column(Float, default=0)
    final_requested_quantity: Mapped[float] = mapped_column(Float, default=0)
    fulfilled_quantity: Mapped[float] = mapped_column(Float, default=0)        # cumulative shipped/confirmed
    received_into_store_count: Mapped[float] = mapped_column(Float, default=0) # cumulative applied to inventory
    status: Mapped[str] = mapped_column(String(32), default=LineStatus.PENDING)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)

    request: Mapped["DailyRequest"] = relationship(back_populates="lines")
    product: Mapped["Product"] = relationship()


class WebhookEvent(Base):
    __tablename__ = "webhook_events"
    __table_args__ = (
        # Each Unleashed event notification is processed at most once.
        UniqueConstraint("provider", "event_notification_id", name="uq_webhook_event"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    provider: Mapped[str] = mapped_column(String(32), default="unleashed")
    event_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    event_notification_id: Mapped[str | None] = mapped_column(String(80), index=True, nullable=True)
    resource_guid: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    raw_payload: Mapped[str | None] = mapped_column(Text, nullable=True)
    received_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(24), default=WebhookStatus.RECEIVED)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    email: Mapped[str] = mapped_column(String(190), unique=True, index=True)
    password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    role: Mapped[str] = mapped_column(String(32), default=Role.EMPLOYEE)
    store_id: Mapped[int | None] = mapped_column(ForeignKey("stores.id"), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)

    store: Mapped["Store | None"] = relationship()


class Setting(Base):
    """Key/value app settings editable from the admin UI (non-secret only).

    Secrets (Unleashed keys) stay in environment variables and are never stored
    here or shown in the UI.
    """

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)
