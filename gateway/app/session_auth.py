"""Stateless signed session cookies for portfolio SSO."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from config import get_runtime_mode, settings

SESSION_COOKIE_NAME = "aadharcha_session"
DEFAULT_SESSION_TTL_HOURS = 24


def _session_secret() -> str:
    secret = (
        getattr(settings, "session_secret", None)
        or os.getenv("SESSION_SECRET")
        or "aadhaarchain-local-dev-session-secret"
    )
    return secret


def _encode_payload(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_payload(encoded: str) -> dict[str, Any]:
    padding = "=" * (-len(encoded) % 4)
    raw = base64.urlsafe_b64decode(f"{encoded}{padding}")
    return json.loads(raw.decode("utf-8"))


def _sign(encoded_payload: str) -> str:
    return hmac.new(
        _session_secret().encode("utf-8"),
        encoded_payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def create_session_token(
    wallet_address: str,
    did: str,
    audience: str,
    ttl_hours: Optional[int] = None,
) -> str:
    ttl = ttl_hours or getattr(settings, "session_ttl_hours", DEFAULT_SESSION_TTL_HOURS)
    expires_at = datetime.now(timezone.utc) + timedelta(hours=ttl)
    payload = {
        "wallet_address": wallet_address,
        "did": did,
        "aud": audience,
        "exp": int(expires_at.timestamp()),
        "sid": secrets.token_urlsafe(12),
    }
    encoded = _encode_payload(payload)
    signature = _sign(encoded)
    return f"{encoded}.{signature}"


def parse_session_token(token: str) -> Optional[dict[str, Any]]:
    if not token or "." not in token:
        return None

    encoded, signature = token.rsplit(".", 1)
    expected = _sign(encoded)
    if not hmac.compare_digest(expected, signature):
        return None

    try:
        payload = _decode_payload(encoded)
    except (json.JSONDecodeError, ValueError, UnicodeDecodeError):
        return None

    exp = payload.get("exp")
    if not isinstance(exp, int) or exp < int(datetime.now(timezone.utc).timestamp()):
        return None

    wallet_address = payload.get("wallet_address")
    if not isinstance(wallet_address, str) or not wallet_address:
        return None

    return payload


def cookie_secure_flag() -> bool:
    return get_runtime_mode() == "production"


def cookie_samesite() -> str:
    return "none" if cookie_secure_flag() else "lax"


def cookie_domain() -> Optional[str]:
    if get_runtime_mode() == "production":
        return ".aadharcha.in"
    return None


def set_session_cookie(response, token: str) -> None:
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        httponly=True,
        secure=cookie_secure_flag(),
        samesite=cookie_samesite(),
        domain=cookie_domain(),
        max_age=getattr(settings, "session_ttl_hours", DEFAULT_SESSION_TTL_HOURS) * 3600,
        path="/",
    )


def clear_session_cookie(response) -> None:
    response.delete_cookie(
        key=SESSION_COOKIE_NAME,
        path="/",
        domain=cookie_domain(),
    )
