"""DB-backed app settings (the `settings` table) + auto-submit schedule config.

Only non-secret operational settings live here — credentials stay in env vars.
"""
from __future__ import annotations

import re
from datetime import date, datetime

import pytz
from sqlalchemy.orm import Session

from ..models import Setting

AUTO_SUBMIT_ENABLED = "auto_submit_enabled"
AUTO_SUBMIT_TIME = "auto_submit_time"          # "HH:MM", 24-hour
AUTO_SUBMIT_TIMEZONE = "auto_submit_timezone"  # IANA name, e.g. America/Los_Angeles

DEFAULT_AUTO_SUBMIT_TIME = "16:45"
DEFAULT_TIMEZONE = "America/Los_Angeles"

_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")

# Offered in the settings UI; any valid IANA name typed via "Other" also works.
COMMON_TIMEZONES = [
    "America/Los_Angeles",
    "America/Denver",
    "America/Phoenix",
    "America/Chicago",
    "America/New_York",
    "Pacific/Honolulu",
    "UTC",
]


def get_setting(db: Session, key: str, default: str | None = None) -> str | None:
    row = db.get(Setting, key)
    return row.value if row is not None and row.value is not None else default


def set_setting(db: Session, key: str, value: str) -> None:
    row = db.get(Setting, key)
    if row is None:
        db.add(Setting(key=key, value=value))
    else:
        row.value = value


def get_auto_submit_config(db: Session) -> dict:
    enabled = (get_setting(db, AUTO_SUBMIT_ENABLED, "false") or "false").lower() in ("1", "true", "yes")
    time_str = get_setting(db, AUTO_SUBMIT_TIME, DEFAULT_AUTO_SUBMIT_TIME) or DEFAULT_AUTO_SUBMIT_TIME
    tz = get_setting(db, AUTO_SUBMIT_TIMEZONE, DEFAULT_TIMEZONE) or DEFAULT_TIMEZONE
    m = _TIME_RE.match(time_str)
    hour, minute = (int(m.group(1)), int(m.group(2))) if m else (16, 45)
    return {"enabled": enabled, "time": time_str, "timezone": tz, "hour": hour, "minute": minute}


def save_auto_submit_config(db: Session, *, enabled: bool, time_str: str, timezone_str: str) -> dict:
    """Validate and persist. Raises ValueError with a friendly message."""
    time_str = time_str.strip()
    if not _TIME_RE.match(time_str):
        raise ValueError(f"'{time_str}' is not a valid time — use 24-hour HH:MM, e.g. 16:45.")
    timezone_str = timezone_str.strip()
    try:
        pytz.timezone(timezone_str)
    except pytz.UnknownTimeZoneError:
        raise ValueError(f"'{timezone_str}' is not a known timezone — use an IANA name like America/Los_Angeles.")
    set_setting(db, AUTO_SUBMIT_ENABLED, "true" if enabled else "false")
    set_setting(db, AUTO_SUBMIT_TIME, time_str)
    set_setting(db, AUTO_SUBMIT_TIMEZONE, timezone_str)
    db.commit()
    return get_auto_submit_config(db)


def local_today(db: Session) -> date:
    """Today's date in the configured business timezone (the server runs UTC —
    at 4:45pm in LA the UTC date may already be tomorrow)."""
    tz = pytz.timezone(get_auto_submit_config(db)["timezone"])
    return datetime.now(tz).date()
