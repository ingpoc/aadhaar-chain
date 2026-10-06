"""Stateless signed session cookies for portfolio SSO."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional

from fastapi import HTTPException, Request

from config import get_runtime_mode, settings

from app.session_registry import is_revoked, register_session

SESSION_COOKIE_NAME = "aadharcha_session"
BUYER_SESSION_COOKIE_NAME = "aadharcha_session_buyer"
SELLER_SESSION_COOKIE_NAME = "aadharcha_session_seller"
DEFAULT_SESSION_TTL_HOURS = 24
AUDIENCE_ALIASES = {
    "ondcbuyer": "buyer",
    "buyer": "buyer",
    "ondcseller": "seller",
    "seller": "seller",
}
SELLER_STEP_UP_ACTIONS = frozenset(
    {
        "seller.refund.issue",
        "seller.catalog.publish",
        "seller.catalog.archive",
        "refund",
    }
)


def _session_secret() -> str:
    secret = (
        getattr(settings, "session_secret", None)
        or os.getenv("SESSION_SECRET")
        or "aadhaarchain-local-dev-session-secret"
    )
    return secret


def _session_secret_previous() -> Optional[str]:
    previous = getattr(settings, "session_secret_previous", None) or os.getenv(
        "SESSION_SECRET_PREVIOUS"
    )
    if not previous:
        return None
    value = str(previous).strip()
    return value or None


def _session_secrets() -> list[str]:
    current = _session_secret()
    secrets_list = [current]
    previous = _session_secret_previous()
    if previous and previous != current:
        secrets_list.append(previous)
    return secrets_list


def canonical_audience(aud: Optional[str]) -> Optional[str]:
    if not isinstance(aud, str):
        return None
    return AUDIENCE_ALIASES.get(aud.strip().lower())


def audiences_match(session_aud: Optional[str], required: Optional[str]) -> bool:
    left = canonical_audience(session_aud)
    right = canonical_audience(required)
    return left is not None and left == right


def session_has_mfa(session: dict[str, Any]) -> bool:
    if session.get("assurance_level") == "mfa":
        return True
    amr = session.get("amr") or []
    if isinstance(amr, str):
        amr = [amr]
    return any(str(item).lower() == "mfa" for item in amr)


def step_up_required(session: dict[str, Any], action: str) -> bool:
    """Seller elevated writes require MFA on staging/production sessions."""
    mode = get_runtime_mode()
    if mode not in ("staging", "production"):
        return False
    normalized = action.strip().lower()
    if normalized not in SELLER_STEP_UP_ACTIONS:
        return False
    return not session_has_mfa(session)


def _encode_payload(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_payload(encoded: str) -> dict[str, Any]:
    padding = "=" * (-len(encoded) % 4)
    raw = base64.urlsafe_b64decode(f"{encoded}{padding}")
    return json.loads(raw.decode("utf-8"))


def _sign_with(encoded_payload: str, secret: str) -> str:
    return hmac.new(
        secret.encode("utf-8"),
        encoded_payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _sign(encoded_payload: str) -> str:
    return _sign_with(encoded_payload, _session_secret())


def _signatures_match(left: str, right: str) -> bool:
    if len(left) != len(right):
        return False
    return hmac.compare_digest(left, right)


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
    amr: Optional[list[str]] = None,
    acr: Optional[str] = None,
    device_label: Optional[str] = None,
) -> str:
    ttl = ttl_hours or getattr(settings, "session_ttl_hours", DEFAULT_SESSION_TTL_HOURS)
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(hours=ttl)
    payload: dict[str, Any] = {
        "principal_id": principal_id,
        "identity_provider": identity_provider,
        "aud": audience,
        "iat": int(now.timestamp()),
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
    if amr:
        payload["amr"] = [str(item) for item in amr]
    if acr:
        payload["acr"] = acr
    encoded = _encode_payload(payload)
    signature = _sign(encoded)
    register_session(payload, device_label=device_label)
    return f"{encoded}.{signature}"


def decode_session_payload(token: str) -> Optional[dict[str, Any]]:
    """Validate HMAC, expiry, and principal without consulting the revoke registry."""
    if not token or "." not in token:
        return None

    encoded, signature = token.rsplit(".", 1)
    if not any(
        _signatures_match(_sign_with(encoded, secret), signature)
        for secret in _session_secrets()
    ):
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


def parse_session_token(token: str) -> Optional[dict[str, Any]]:
    payload = decode_session_payload(token)
    if payload is None or is_revoked(payload):
        return None
    return payload


_AUDIENCE_COOKIE_NAMES = {
    "buyer": BUYER_SESSION_COOKIE_NAME,
    "seller": SELLER_SESSION_COOKIE_NAME,
}
ALL_SESSION_COOKIE_NAMES = (
    SESSION_COOKIE_NAME,
    BUYER_SESSION_COOKIE_NAME,
    SELLER_SESSION_COOKIE_NAME,
)
# Local dev SPA ports (vite.config.ts in ondcbuyer / ondcseller).
_DEV_PORT_AUDIENCES = {43102: "buyer", 43103: "seller"}


def audience_cookie_name(aud: Optional[str]) -> Optional[str]:
    """Role-scoped cookie for an audience, so Buyer and Seller sessions coexist."""
    canonical = canonical_audience(aud)
    return _AUDIENCE_COOKIE_NAMES.get(canonical) if canonical else None


def audience_from_origin(request: Request) -> Optional[str]:
    """Which portfolio app is calling, from the browser-set Origin header.

    Only a hint for choosing between the caller's own session cookies; every
    token is still signature-, expiry-, revocation- and audience-checked.
    """
    from urllib.parse import urlparse

    origin = (request.headers.get("origin") or "").strip()
    if not origin or origin == "null":
        return None
    parsed = urlparse(origin)
    host = (parsed.hostname or "").lower()
    first_label = host.split(".", 1)[0]
    if first_label in ("ondcbuyer", "ondcseller"):
        return canonical_audience(first_label)
    if host in ("localhost", "127.0.0.1"):
        return _DEV_PORT_AUDIENCES.get(parsed.port or 0)
    return None


def resolve_session(
    request: Request, audience: Optional[str] = None
) -> Optional[dict[str, Any]]:
    """Session for ``audience`` (or the calling app), else the legacy cookie.

    The role-scoped cookie is used only when its own token's audience matches
    the requested role. Otherwise the legacy shared cookie is returned
    unchanged, so callers keep enforcing their existing audience checks.
    """
    wanted = canonical_audience(audience) if audience else audience_from_origin(request)
    name = audience_cookie_name(wanted)
    if name:
        scoped = parse_session_token(request.cookies.get(name, ""))
        if scoped and audiences_match(scoped.get("aud"), wanted):
            return scoped
    return parse_session_token(request.cookies.get(SESSION_COOKIE_NAME, ""))


def require_buyer_principal(request: Request) -> str:
    """Buyer session, plus Seller social handoff used by commerce (#16/#19/#21)."""
    session = resolve_session(request, "buyer")
    if not session or not session.get("principal_id"):
        raise HTTPException(status_code=401, detail="Authenticated principal required.")
    shared_social_session = (
        session.get("identity_provider") in {"auth0", "google"}
        and session.get("aud") == "ondcseller"
    )
    if session.get("aud") != "ondcbuyer" and not shared_social_session:
        raise HTTPException(status_code=403, detail="Buyer session required.")
    return str(session["principal_id"])


def session_user_payload(session: dict[str, Any]) -> dict[str, Any]:
    """Shape returned by /api/auth/me and /api/auth/validate."""
    principal_id = session.get("principal_id")
    wallet_address = session.get("wallet_address")
    if not principal_id and wallet_address:
        principal_id = f"wallet:{wallet_address}"
    provider = session.get("identity_provider") or "wallet"
    if session_has_mfa(session):
        assurance = "mfa"
    elif provider == "demo":
        assurance = "demo"
    else:
        assurance = "social"
    data: dict[str, Any] = {
        "principal_id": principal_id,
        "identity_provider": provider,
        "assurance_level": assurance,
        "audience": session.get("aud"),
        "sid": session.get("sid"),
    }
    if session.get("amr"):
        data["amr"] = session["amr"]
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
    """Write the legacy shared cookie plus the token audience's scoped cookie."""
    names = [SESSION_COOKIE_NAME]
    payload = decode_session_payload(token)
    scoped = audience_cookie_name((payload or {}).get("aud"))
    if scoped:
        names.append(scoped)
    for name in names:
        response.set_cookie(
            key=name,
            value=token,
            httponly=True,
            secure=cookie_secure_flag(),
            samesite=cookie_samesite(),
            domain=cookie_domain(),
            max_age=getattr(settings, "session_ttl_hours", DEFAULT_SESSION_TTL_HOURS)
            * 3600,
            path="/",
        )


def clear_session_cookie(response, names: Optional[Iterable[str]] = None) -> None:
    """Clear session cookies (all of them unless specific names are given)."""
    for name in names or ALL_SESSION_COOKIE_NAMES:
        response.delete_cookie(
            key=name,
            path="/",
            domain=cookie_domain(),
            secure=cookie_secure_flag(),
            samesite=cookie_samesite(),
        )


def session_cookie_sids(request: Request) -> dict[str, str]:
    """Map each present, signature-valid session cookie name to its sid."""
    sids: dict[str, str] = {}
    for name in ALL_SESSION_COOKIE_NAMES:
        payload = decode_session_payload(request.cookies.get(name, ""))
        if payload and payload.get("sid"):
            sids[name] = str(payload["sid"])
    return sids
