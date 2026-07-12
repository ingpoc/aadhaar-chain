"""ONDC site verification + /on_subscribe hosting (staging onboarding).

Serves paths that Vercel rewrites from Buyer/Seller FQDNs:
  GET  /ondc/np/{role}/ondc-site-verification.html
  POST /ondc/np/{role}/on_subscribe

Also mounts FQDN-shaped aliases when Host matches configured subscriber_id
(useful once PUBLIC_GATEWAY_URL is the NP host or for local tunnel tests).

Keys (priority):
  1. Render/env PEM secrets → materialize under /tmp/ondc-env/{role}/ (ephemeral FS safe)
  2. ONDC_{ROLE}_KEYS_DIR override
  3. portal-download/{buyer,seller}/ under .local/ondc-sandbox (local PreProd)
  4. {buyer,seller}/ local DER sandbox

Env (prefer *_PEM_B64 on Render; never commit values):
  ONDC_BUYER_SIGNING_PRIVATE_PEM[_B64], ONDC_BUYER_ENCRYPTION_PRIVATE_PEM[_B64]
  ONDC_SELLER_SIGNING_PRIVATE_PEM[_B64], ONDC_SELLER_ENCRYPTION_PRIVATE_PEM[_B64]
  ONDC_{BUYER|SELLER}_REQUEST_ID, ONDC_{BUYER|SELLER}_UNIQUE_KEY_ID
  ONDC_{BUYER|SELLER}_PUBLIC_METADATA_JSON (optional)

Does not flip ONDC_ENABLED / commerce demo mode.
"""
from __future__ import annotations

import base64
import json
import os
import uuid
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from config import settings

router = APIRouter(tags=["ondc-onboard"])

# ONDC registry public encryption keys (Onboarding of Participants §6)
ONDC_ENC_PUBLIC_KEYS = {
    "staging": "MCowBQYDK2VuAyEAduMuZgmtpjdCuxv+Nc49K0cB6tL/Dj3HZetvVN7ZekM=",
    "preprod": "MCowBQYDK2VuAyEAa9Wbpvd9SsrpOZFcynyt/TO3x0Yrqyys4NUGIvyxX2Q=",
    "prod": "MCowBQYDK2VuAyEAvVEyZY91O2yV8w8/CAwVDAnqIZDJJUPdLUUKwLo3K0M=",
}

_GATEWAY_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_SANDBOX = _GATEWAY_ROOT / ".local" / "ondc-sandbox"
_ENV_KEYS_ROOT = Path(os.environ.get("ONDC_ENV_KEYS_DIR", "/tmp/ondc-env"))
_ENV_MATERIALIZED: set[str] = set()


class OnSubscribeBody(BaseModel):
    subscriber_id: Optional[str] = None
    challenge: str = Field(..., min_length=1)


def _ondc_env() -> str:
    env = (getattr(settings, "ondc_registry_env", None) or "staging").lower().strip()
    if env not in ONDC_ENC_PUBLIC_KEYS:
        return "staging"
    return env


def _role_env_prefix(role: str) -> str:
    return f"ONDC_{role.upper()}"


def _decode_pem_env(raw: str) -> bytes:
    """Accept raw PEM text or base64(PEM text/bytes)."""
    text = raw.strip()
    if "BEGIN" in text:
        return text.encode("utf-8") if not text.endswith("\n") else text.encode("utf-8")
    try:
        decoded = base64.b64decode(text, validate=False)
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"invalid PEM/base64: {exc}") from exc
    if b"BEGIN" in decoded:
        return decoded if decoded.endswith(b"\n") else decoded + b"\n"
    # Already DER — wrap is not expected; require PEM
    raise ValueError("decoded value is not PEM")


def _read_role_pem_env(role: str, kind: str) -> Optional[bytes]:
    """kind: signing | encryption. Checks ONDC_{ROLE}_{KIND}_PRIVATE_PEM[_B64]."""
    prefix = _role_env_prefix(role)
    kind_u = kind.upper()
    for key in (
        f"{prefix}_{kind_u}_PRIVATE_PEM",
        f"{prefix}_{kind_u}_PRIVATE_PEM_B64",
        f"{prefix}_{kind_u}_PRIVATE_KEY_PEM",
        f"{prefix}_{kind_u}_PRIVATE_KEY_PEM_B64",
    ):
        raw = os.environ.get(key)
        if raw and raw.strip():
            return _decode_pem_env(raw)
    return None


def _materialize_env_keys(role: str) -> Optional[Path]:
    """Write env PEM secrets to /tmp (Render ephemeral-safe). Returns dir if ready."""
    role = role.lower().strip()
    signing = _read_role_pem_env(role, "signing")
    encryption = _read_role_pem_env(role, "encryption")
    if not signing or not encryption:
        return None
    out = _ENV_KEYS_ROOT / role
    if role in _ENV_MATERIALIZED and (out / "signing_private.pem").is_file():
        return out
    out.mkdir(parents=True, exist_ok=True)
    (out / "signing_private.pem").write_bytes(
        signing if signing.endswith(b"\n") else signing + b"\n"
    )
    (out / "encryption_private.pem").write_bytes(
        encryption if encryption.endswith(b"\n") else encryption + b"\n"
    )
    prefix = _role_env_prefix(role)
    request_id = (os.environ.get(f"{prefix}_REQUEST_ID") or "").strip()
    if request_id:
        (out / "request_id.txt").write_text(request_id + "\n", encoding="utf-8")
    uk_id = (os.environ.get(f"{prefix}_UNIQUE_KEY_ID") or "").strip()
    if uk_id:
        (out / "unique_key_id.txt").write_text(uk_id + "\n", encoding="utf-8")
    meta_raw = (os.environ.get(f"{prefix}_PUBLIC_METADATA_JSON") or "").strip()
    if meta_raw:
        (out / "public_metadata.json").write_text(meta_raw + "\n", encoding="utf-8")
    elif not (out / "public_metadata.json").is_file():
        meta = {
            "source": "env",
            "role": role,
            "unique_key_id": uk_id or None,
            "note": "Materialized from ONDC_*_PRIVATE_PEM[_B64] env on boot",
        }
        (out / "public_metadata.json").write_text(
            json.dumps(meta, indent=2) + "\n", encoding="utf-8"
        )
    _ENV_MATERIALIZED.add(role)
    return out


def _resolve_keys_base(role: str) -> Path:
    """Prefer env PEMs (Render), else settings dir, else portal PreProd, else local DER."""
    env_dir = _materialize_env_keys(role)
    if env_dir is not None:
        return env_dir
    configured = getattr(settings, f"ondc_{role}_keys_dir", None)
    if configured:
        return Path(configured).expanduser()
    portal = _DEFAULT_SANDBOX / "portal-download" / role
    local = _DEFAULT_SANDBOX / role
    source = (getattr(settings, "ondc_keys_source", None) or "auto").lower().strip()
    portal_ready = (portal / "encryption_private.pem").is_file() and (
        portal / "signing_private.pem"
    ).is_file()
    local_ready = (local / "encryption_private.pem").is_file() and (
        local / "signing_private.pem"
    ).is_file()
    if source == "portal":
        return portal
    if source == "local":
        return local
    # auto: PreProd portal subscribed ⇒ prefer portal PEMs when present
    env = _ondc_env()
    if env == "preprod" and portal_ready:
        return portal
    if portal_ready and not local_ready:
        return portal
    return local if local_ready else portal


def _role_paths(role: str) -> dict[str, Path]:
    role = role.lower().strip()
    if role not in {"buyer", "seller"}:
        raise HTTPException(status_code=404, detail="role must be buyer|seller")
    base = _resolve_keys_base(role)
    return {
        "base": base,
        "signing_pem": base / "signing_private.pem",
        "encryption_pem": base / "encryption_private.pem",
        "meta": base / "public_metadata.json",
        "request_id": base / "request_id.txt",
        "uk_id": base / "unique_key_id.txt",
    }


def _ensure_request_id(paths: dict[str, Path]) -> str:
    path = paths["request_id"]
    if path.is_file():
        value = path.read_text(encoding="utf-8").strip()
        if value:
            return value
    value = f"aadhaar-{uuid.uuid4()}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value + "\n", encoding="utf-8")
    return value


def _load_signing_private(paths: dict[str, Path]):
    from cryptography.hazmat.primitives import serialization

    pem = paths["signing_pem"]
    if not pem.is_file():
        raise HTTPException(
            status_code=503,
            detail=f"Missing signing private key at {pem}",
        )
    return serialization.load_pem_private_key(pem.read_bytes(), password=None)


def _load_encryption_private(paths: dict[str, Path]):
    from cryptography.hazmat.primitives import serialization

    pem = paths["encryption_pem"]
    if not pem.is_file():
        raise HTTPException(
            status_code=503,
            detail=f"Missing encryption private key at {pem}",
        )
    return serialization.load_pem_private_key(pem.read_bytes(), password=None)


def _sign_request_id(request_id: str, signing_private) -> str:
    """Ed25519 sign request_id bytes without hashing (ONDC domain verification)."""
    signature = signing_private.sign(request_id.encode("utf-8"))
    return base64.b64encode(signature).decode("ascii")


def _pkcs7_unpad(data: bytes) -> bytes:
    if not data:
        raise ValueError("empty decrypt")
    pad_len = data[-1]
    if pad_len < 1 or pad_len > 16 or data[-pad_len:] != bytes([pad_len]) * pad_len:
        raise ValueError("bad PKCS7 padding")
    return data[:-pad_len]


def _decrypt_challenge(challenge_b64: str, enc_private, ondc_public_b64: str) -> str:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    ondc_public = serialization.load_der_public_key(base64.b64decode(ondc_public_b64))
    shared = enc_private.exchange(ondc_public)
    cipher = Cipher(algorithms.AES(shared), modes.ECB())
    decryptor = cipher.decryptor()
    plain = decryptor.update(base64.b64decode(challenge_b64)) + decryptor.finalize()
    return _pkcs7_unpad(plain).decode("utf-8")


def _verification_html(role: str) -> HTMLResponse:
    paths = _role_paths(role)
    request_id = _ensure_request_id(paths)
    signature = _sign_request_id(request_id, _load_signing_private(paths))
    html = f"""<!DOCTYPE html>
<html>
<head>
<meta name="ondc-site-verification" content="{signature}" />
<title>ONDC Site Verification</title>
</head>
<body>
ONDC Site Verification Page
</body>
</html>
"""
    return HTMLResponse(content=html, media_type="text/html")


async def _on_subscribe(role: str, body: OnSubscribeBody) -> JSONResponse:
    paths = _role_paths(role)
    env = _ondc_env()
    try:
        answer = _decrypt_challenge(
            body.challenge,
            _load_encryption_private(paths),
            ONDC_ENC_PUBLIC_KEYS[env],
        )
    except Exception as exc:  # noqa: BLE001 — return clear 400 to registry
        raise HTTPException(
            status_code=400,
            detail=f"on_subscribe decrypt failed: {exc}",
        ) from exc
    return JSONResponse({"answer": answer})


@router.get("/ondc/np/{role}/ondc-site-verification.html")
async def np_site_verification(role: str) -> HTMLResponse:
    return _verification_html(role)


@router.post("/ondc/np/{role}/on_subscribe")
async def np_on_subscribe(role: str, body: OnSubscribeBody) -> JSONResponse:
    return await _on_subscribe(role, body)


def _host_role(host: str) -> Optional[str]:
    host = (host or "").split(":")[0].lower()
    buyer = (getattr(settings, "ondc_buyer_subscriber_id", None) or "ondcbuyer.aadharcha.in").lower()
    seller = (getattr(settings, "ondc_seller_subscriber_id", None) or "ondcseller.aadharcha.in").lower()
    if host == buyer:
        return "buyer"
    if host == seller:
        return "seller"
    return None


@router.get("/ondc-site-verification.html")
async def root_site_verification(request: Request) -> HTMLResponse:
    role = _host_role(request.headers.get("host", ""))
    if role is None:
        raise HTTPException(
            status_code=404,
            detail="Host not mapped; use /ondc/np/{buyer|seller}/ondc-site-verification.html",
        )
    return _verification_html(role)


@router.post("/ondc/on_subscribe")
async def root_on_subscribe(request: Request, body: OnSubscribeBody) -> JSONResponse:
    role = _host_role(request.headers.get("host", ""))
    if role is None and body.subscriber_id:
        # Fall back: match subscriber_id from body
        sid = body.subscriber_id.lower().strip()
        buyer = (getattr(settings, "ondc_buyer_subscriber_id", None) or "ondcbuyer.aadharcha.in").lower()
        seller = (getattr(settings, "ondc_seller_subscriber_id", None) or "ondcseller.aadharcha.in").lower()
        if sid == buyer:
            role = "buyer"
        elif sid == seller:
            role = "seller"
    if role is None:
        raise HTTPException(
            status_code=404,
            detail="Host/subscriber not mapped; use /ondc/np/{buyer|seller}/on_subscribe",
        )
    return await _on_subscribe(role, body)


@router.get("/ondc/np/{role}/status")
async def np_onboard_status(role: str) -> JSONResponse:
    paths = _role_paths(role)
    meta: dict[str, Any] = {}
    if paths["meta"].is_file():
        try:
            meta = json.loads(paths["meta"].read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            meta = {"error": "invalid public_metadata.json"}
    request_id = paths["request_id"].read_text(encoding="utf-8").strip() if paths["request_id"].is_file() else None
    uk_id = None
    if paths["uk_id"].is_file():
        uk_id = paths["uk_id"].read_text(encoding="utf-8").strip() or None
    uk_id = uk_id or meta.get("unique_key_id")
    return JSONResponse(
        {
            "success": True,
            "data": {
                "role": role,
                "keys_dir": str(paths["base"]),
                "keys_source": meta.get("source")
                or (
                    "env"
                    if str(paths["base"]).startswith(str(_ENV_KEYS_ROOT))
                    else (
                        "portal-download"
                        if "portal-download" in str(paths["base"])
                        else "local"
                    )
                ),
                "signing_key_present": paths["signing_pem"].is_file(),
                "encryption_key_present": paths["encryption_pem"].is_file(),
                "request_id": request_id,
                "unique_key_id": uk_id,
                "encryption_public_key_format": meta.get("encryption_public_key_format"),
                "signing_public_key_b64": meta.get("signing_public_key_b64"),
                "encryption_public_key_b64": meta.get("encryption_public_key_b64"),
                "registry_env": _ondc_env(),
                "callback_url": "/ondc",
                "note": "Wire Vercel rewrites to these paths; deploy PUBLIC gateway before registry challenge.",
            },
        }
    )
