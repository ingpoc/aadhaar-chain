"""Durable Google OAuth state — HMAC-signed payload (multi-instance safe)."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Any
from urllib.parse import urlparse

from config import settings


OAUTH_STATE_TTL_SEC = 600


def _secret() -> bytes:
    secret = (
        getattr(settings, "session_secret", None)
        or "aadhaarchain-local-dev-session-secret"
    )
    return str(secret).encode("utf-8")


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _unb64url(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(f"{data}{padding}")


def mint_oauth_state(*, return_url: str, aud: str) -> str:
    payload = {
        "return_url": return_url,
        "aud": aud,
        "exp": int(time.time()) + OAUTH_STATE_TTL_SEC,
    }
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    encoded = _b64url(raw)
    sig = hmac.new(_secret(), encoded.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{encoded}.{sig}"


def parse_oauth_state(state: str) -> dict[str, Any]:
    try:
        encoded, sig = state.rsplit(".", 1)
    except ValueError as exc:
        raise ValueError("Malformed OAuth state") from exc
    expect = hmac.new(_secret(), encoded.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expect, sig):
        raise ValueError("Invalid OAuth state signature")
    payload = json.loads(_unb64url(encoded).decode("utf-8"))
    if int(payload.get("exp") or 0) < int(time.time()):
        raise ValueError("OAuth state expired")
    if not payload.get("return_url") or not payload.get("aud"):
        raise ValueError("OAuth state missing fields")
    return payload


def is_allowed_return_url(return_url: str) -> bool:
    """Allow returns only to configured CORS origins / localhost portfolio ports."""
    try:
        parsed = urlparse(return_url)
    except Exception:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    if not parsed.netloc:
        return False
    origin = f"{parsed.scheme}://{parsed.netloc}"
    allowed: list[str] = []
    cors = getattr(settings, "cors_origins", None) or []
    if isinstance(cors, str):
        allowed.extend(o.strip() for o in cors.split(",") if o.strip())
    else:
        allowed.extend(str(o).rstrip("/") for o in cors)
    public_web = (getattr(settings, "public_web_url", None) or "").rstrip("/")
    if public_web:
        allowed.append(public_web)
    # Local portfolio defaults
    for host in ("127.0.0.1", "localhost"):
        for port in (43100, 43102, 43103, 43105):
            allowed.append(f"http://{host}:{port}")
    normalized = {a.rstrip("/") for a in allowed if a}
    return origin.rstrip("/") in normalized
