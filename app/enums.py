"""Enumerations shared across the app.

Kept as plain string constants (not Python Enums) so they serialise cleanly to
the database, JSON and templates without conversion friction.
"""
from __future__ import annotations


class TransactionType:
    """Local ledger transaction types. Every inventory movement creates one."""

    STORE_REMOVAL = "STORE_REMOVAL"                    # employee took stock for café use (delta < 0)
    SCAN_UNDO = "SCAN_UNDO"                            # employee undid a just-made removal (delta > 0)
    UNLEASHED_RECEIPT = "UNLEASHED_RECEIPT"            # fulfilled qty received (delta > 0)
    COUNT_ADJUSTMENT = "COUNT_ADJUSTMENT"              # manager correction (delta any)
    # Audit-only markers (delta == 0), recorded for a full ledger:
    DAILY_REQUEST_GENERATED = "DAILY_REQUEST_GENERATED"
    UNLEASHED_REQUEST_SUBMITTED = "UNLEASHED_REQUEST_SUBMITTED"
    REQUEST_OVERRIDE = "REQUEST_OVERRIDE"
    REQUEST_CANCELLED = "REQUEST_CANCELLED"
    SYNC_ERROR = "SYNC_ERROR"

    INVENTORY_AFFECTING = {STORE_REMOVAL, SCAN_UNDO, UNLEASHED_RECEIPT, COUNT_ADJUSTMENT}


class RequestStatus:
    """DailyRequest lifecycle."""

    DRAFT = "DRAFT"                 # generated, editable by a manager
    SUBMITTED = "SUBMITTED"         # sent to Unleashed as a Sales Order
    SYNC_ERROR = "SYNC_ERROR"       # submission to Unleashed failed (retryable)
    COMPLETED = "COMPLETED"         # Unleashed order detected Completed (receipt pending)
    RECEIVED = "RECEIVED"           # fulfilled quantities applied to local inventory
    RECEIPT_ERROR = "RECEIPT_ERROR" # completion detected but receipt processing failed (retryable)
    CANCELLED = "CANCELLED"         # retired locally; any Unleashed order must be removed there too

    OPEN_FOR_RECEIPT = {SUBMITTED, COMPLETED, RECEIPT_ERROR}
    CANCELLABLE = {DRAFT, SUBMITTED, SYNC_ERROR, COMPLETED, RECEIPT_ERROR}


class LineStatus:
    PENDING = "PENDING"
    SUBMITTED = "SUBMITTED"
    RECEIVED = "RECEIVED"


class Role:
    EMPLOYEE = "EMPLOYEE"
    STORE_MANAGER = "STORE_MANAGER"
    WAREHOUSE = "WAREHOUSE"
    ADMIN = "ADMIN"

    ALL = {EMPLOYEE, STORE_MANAGER, WAREHOUSE, ADMIN}


class WebhookStatus:
    RECEIVED = "RECEIVED"
    PROCESSED = "PROCESSED"
    IGNORED = "IGNORED"
    ERROR = "ERROR"


# Unleashed sales order status that means "done" and should trigger a receipt.
UNLEASHED_COMPLETED_STATUS = "Completed"
