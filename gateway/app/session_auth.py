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
    """Legacy wallet SSO session (compatibility). Prefer create_principal_session_token."""
    return create_principal_session_token(
        principal_id=f"wallet:{wallet_address}",
        audience=audience,
        identity_provider="wallet",
        display_name=None,
        email=None,
        wallet_address=wallet_address,
        did=did,
        ttl_hours=ttl_hours,
    )


def create_principal_session_token(
    *,
    principal_id: str,
    audience: str,
    identity_provider: str,
    display_name: Optional[str] = None,
    email: Optional[str] = None,
    wallet_address: Optional[str] = None,
    did: Optional[str] = None,
    ttl_hours: Optional[int] = None,
) -> str:
    ttl = ttl_hours or getattr(settings, "session_ttl_hours", DEFAULT_SESSION_TTL_HOURS)
    expires_at = datetime.now(timezone.utc) + timedelta(hours=ttl)
    payload: dict[str, Any] = {
        "principal_id": principal_id,
        "identity_provider": identity_provider,
        "aud": audience,
        "exp": int(expires_at.timestamp()),
        "sid": secrets.token_urlsafe(12),
    }
    if display_name:
        payload["display_name"] = display_name
    if email:
        payload["email"] = email
    if wallet_address:
        payload["wallet_address"] = wallet_address
    if did:
        payload["did"] = did
    elif wallet_address:
        payload["did"] = f"did:solana:{wallet_address}"
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

    principal_id = payload.get("principal_id")
    wallet_address = payload.get("wallet_address")
    has_principal = isinstance(principal_id, str) and bool(principal_id)
    has_wallet = isinstance(wallet_address, str) and bool(wallet_address)
    if not has_principal and not has_wallet:
        return None
    if has_wallet and not has_principal:
        payload["principal_id"] = f"wallet:{wallet_address}"
        payload.setdefault("identity_provider", "wallet")
    return payload


def session_user_payload(session: dict[str, Any]) -> dict[str, Any]:
    """Shape returned by /api/auth/me and /api/auth/validate."""
    principal_id = session.get("principal_id")
    wallet_address = session.get("wallet_address")
    if not principal_id and wallet_address:
        principal_id = f"wallet:{wallet_address}"
    data: dict[str, Any] = {
        "principal_id": principal_id,
        "identity_provider": session.get("identity_provider") or "wallet",
        "assurance_level": "demo" if session.get("identity_provider") == "demo" else "social",
        "audience": session.get("aud"),
    }
    if session.get("display_name"):
        data["display_name"] = session["display_name"]
    if session.get("email"):
        data["email"] = session["email"]
    # Legacy field for migrating clients — omit when absent.
    if wallet_address:
        data["wallet_address"] = wallet_address
        data["did"] = session.get("did") or f"did:solana:{wallet_address}"
    return data


def _public_gateway_host() -> str:
    from urllib.parse import urlparse

    return (urlparse(settings.public_gateway_url or "").hostname or "").lower()


def cookie_secure_flag() -> bool:
    """Secure cookies for staging/prod and any HTTPS public gateway."""
    mode = get_runtime_mode()
    if mode in ("production", "staging"):
        return True
    return (settings.public_gateway_url or "").strip().lower().startswith("https://")


def cookie_samesite() -> str:
    # Cross-site SPA (Vercel FQDNs) → gateway (Render) needs SameSite=None + Secure.
    return "none" if cookie_secure_flag() else "lax"


def cookie_domain() -> Optional[str]:
    """Set Domain only when the gateway host itself is under aadharcha.in.

    Render host `*.onrender.com` cannot emit Domain=.aadharcha.in (browser rejects).
    Host-only cookies on the gateway origin still work with credentials + CORS.
    """
    host = _public_gateway_host()
    if host == "aadharcha.in" or host.endswith(".aadharcha.in"):
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
        secure=cookie_secure_flag(),
        samesite=cookie_samesite(),
    )
