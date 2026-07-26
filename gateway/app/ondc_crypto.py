"""ONDC Authorization header (Ed25519 + BLAKE-512 digest).

Follows ONDC-Official signing_and_verification (Python cryptic_utils).
Uses cryptography only (no PyNaCl).
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import time
from typing import Any, Optional, Union

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)


def minify_json(payload: Union[str, bytes, dict[str, Any], list[Any]]) -> str:
    """Canonical body string for digest — no spaces."""
    if isinstance(payload, bytes):
        text = payload.decode("utf-8")
        # Re-minify if already JSON text
        try:
            return json.dumps(
                json.loads(text), separators=(",", ":"), ensure_ascii=False
            )
        except json.JSONDecodeError:
            return text
    if isinstance(payload, str):
        try:
            return json.dumps(
                json.loads(payload), separators=(",", ":"), ensure_ascii=False
            )
        except json.JSONDecodeError:
            return payload
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False)


def blake2b_digest_b64(body: str) -> str:
    digest = hashlib.blake2b(body.encode("utf-8"), digest_size=64).digest()
    return base64.b64encode(digest).decode("ascii")


def create_signing_string(digest_b64: str, created: int, expires: int) -> str:
    return (
        f"(created): {created}\n"
        f"(expires): {expires}\n"
        f"digest: BLAKE-512={digest_b64}"
    )


def sign_ed25519(private_key: Ed25519PrivateKey, message: str) -> str:
    sig = private_key.sign(message.encode("utf-8"))
    return base64.b64encode(sig).decode("ascii")


def verify_ed25519(
    public_key: Ed25519PublicKey, message: str, signature_b64: str
) -> bool:
    try:
        public_key.verify(base64.b64decode(signature_b64), message.encode("utf-8"))
        return True
    except Exception:  # noqa: BLE001
        return False


def create_authorization_header(
    body: Union[str, bytes, dict[str, Any]],
    *,
    subscriber_id: str,
    unique_key_id: str,
    private_key: Ed25519PrivateKey,
    created: Optional[int] = None,
    expires: Optional[int] = None,
) -> str:
    """Build Authorization value for ONDC registry/gateway requests."""
    created_ts = int(time.time()) if created is None else int(created)
    expires_ts = created_ts + 3600 if expires is None else int(expires)
    body_str = minify_json(body)
    digest = blake2b_digest_b64(body_str)
    signing_string = create_signing_string(digest, created_ts, expires_ts)
    signature = sign_ed25519(private_key, signing_string)
    key_id = f"{subscriber_id}|{unique_key_id}|ed25519"
    return (
        f'Signature keyId="{key_id}",algorithm="ed25519",'
        f'created="{created_ts}",expires="{expires_ts}",'
        f'headers="(created) (expires) digest",signature="{signature}"'
    )


def verify_authorization_header(
    body: Union[str, bytes, dict[str, Any]],
    header: str,
    *,
    signing_public_key_b64: str,
    expected_subscriber_id: str,
    expected_unique_key_id: str,
    now: Optional[int] = None,
) -> bool:
    """Verify an ONDC Signature header against an exact registry identity."""
    if not header.startswith("Signature "):
        return False
    params = dict(re.findall(r'([A-Za-z_]+)="([^"]*)"', header[10:]))
    key_parts = params.get("keyId", "").split("|")
    if key_parts != [expected_subscriber_id, expected_unique_key_id, "ed25519"]:
        return False
    if (
        params.get("algorithm") != "ed25519"
        or params.get("headers") != "(created) (expires) digest"
    ):
        return False
    try:
        created = int(params["created"])
        expires = int(params["expires"])
        current = int(time.time()) if now is None else int(now)
        public_key = Ed25519PublicKey.from_public_bytes(
            base64.b64decode(signing_public_key_b64)
        )
    except (KeyError, TypeError, ValueError):
        return False
    if created > current + 300 or expires < current or expires <= created:
        return False
    signing_string = create_signing_string(
        blake2b_digest_b64(minify_json(body)), created, expires
    )
    return verify_ed25519(public_key, signing_string, params.get("signature", ""))


def load_ed25519_private_pem(pem_bytes: bytes) -> Ed25519PrivateKey:
    from cryptography.hazmat.primitives import serialization

    key = serialization.load_pem_private_key(pem_bytes, password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise TypeError(f"expected Ed25519 private key, got {type(key)}")
    return key
