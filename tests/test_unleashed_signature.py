"""The HMAC-SHA256 signature must match Unleashed's spec exactly."""
from __future__ import annotations

import base64
import hashlib
import hmac

from app.integrations.unleashed import compute_signature


def _reference(query: str, key: str) -> str:
    return base64.b64encode(
        hmac.new(key.encode("utf-8"), query.encode("utf-8"), hashlib.sha256).digest()
    ).decode("utf-8")


def test_signature_matches_reference_for_query():
    query = "customerCode=ACME"
    assert compute_signature(query, "secret-key") == _reference(query, "secret-key")


def test_signature_for_empty_query():
    # Requests with no query string sign the empty string.
    assert compute_signature("", "secret-key") == _reference("", "secret-key")


def test_signature_is_base64_and_stable():
    sig = compute_signature("pageSize=200&orderStatus=Completed", "k")
    # decodes cleanly as base64 (32-byte SHA256 digest)
    assert len(base64.b64decode(sig)) == 32
    # deterministic
    assert sig == compute_signature("pageSize=200&orderStatus=Completed", "k")
