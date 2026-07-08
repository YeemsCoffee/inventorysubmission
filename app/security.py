"""Auth helpers.

Password hashing uses stdlib PBKDF2-HMAC-SHA256 (no native build deps). Sessions
are signed cookies via Starlette's SessionMiddleware. Role checks are exposed as
FastAPI dependencies.
"""
from __future__ import annotations

import hashlib
import hmac
import os
from base64 import b64decode, b64encode

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from .database import get_db
from .enums import Role
from .models import User

_PBKDF2_ROUNDS = 200_000


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ROUNDS)
    return f"pbkdf2_sha256${_PBKDF2_ROUNDS}${b64encode(salt).decode()}${b64encode(dk).decode()}"


def verify_password(password: str, stored: str | None) -> bool:
    if not stored:
        return False
    try:
        algo, rounds, salt_b64, hash_b64 = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        salt = b64decode(salt_b64)
        expected = b64decode(hash_b64)
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(rounds))
        return hmac.compare_digest(dk, expected)
    except (ValueError, TypeError):
        return False


def sign_token(purpose: str, value: str | int) -> str:
    """Compact HMAC token binding `value` to a named purpose (e.g. a signed
    undo link). Purposes are namespaced so tokens can never be replayed from
    one feature to another."""
    from .config import get_settings  # local import: settings cache is built lazily

    secret = get_settings().secret_key.encode("utf-8")
    msg = f"{purpose}:{value}".encode("utf-8")
    return hmac.new(secret, msg, hashlib.sha256).hexdigest()[:24]


def verify_token(purpose: str, value: str | int, token: str) -> bool:
    return hmac.compare_digest(token, sign_token(purpose, value))


def get_current_user(request: Request, db: Session = Depends(get_db)) -> User | None:
    user_id = request.session.get("user_id")
    if not user_id:
        return None
    return db.get(User, user_id)


def require_roles(*roles: str):
    """Dependency factory enforcing that the logged-in user has one of `roles`."""

    allowed = set(roles)

    def _dep(request: Request, db: Session = Depends(get_db)) -> User:
        user = get_current_user(request, db)
        if user is None or not user.active:
            raise HTTPException(
                status_code=status.HTTP_303_SEE_OTHER,
                headers={"Location": f"/login?next={request.url.path}"},
            )
        if allowed and user.role not in allowed and user.role != Role.ADMIN:
            # Admins implicitly pass every role gate.
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient role")
        return user

    return _dep
