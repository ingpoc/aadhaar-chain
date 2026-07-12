"""HMAC-signed AgentGuard receipts."""
from __future__ import annotations

import hmac
import os
from hashlib import sha256
from typing import Any

from app.agentguard_contract import canonicalize

DEFAULT_RECEIPT_SECRET = "agentguard-local-dev-receipt-secret"
DEFAULT_ISSUER_KEY_ID = "agentguard-local-dev-v1"


def _receipt_secret() -> str:
    return os.getenv("AGENTGUARD_RECEIPT_SECRET") or DEFAULT_RECEIPT_SECRET


def issuer_key_id() -> str:
    return os.getenv("AGENTGUARD_RECEIPT_ISSUER_KEY_ID") or DEFAULT_ISSUER_KEY_ID


def _signable_payload(receipt: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in receipt.items()
        if key not in {"signature", "issuer_key_id"} and value is not None
    }


def sign_receipt(receipt: dict[str, Any]) -> dict[str, Any]:
    payload = _signable_payload(receipt)
    signature = hmac.new(
        _receipt_secret().encode("utf-8"),
        canonicalize(payload).encode("utf-8"),
        sha256,
    ).hexdigest()
    return {**receipt, "issuer_key_id": issuer_key_id(), "signature": signature}


def verify_receipt(receipt: dict[str, Any]) -> dict[str, Any]:
    signature = receipt.get("signature")
    if not isinstance(signature, str) or not signature:
        return {"valid": False, "reason": "missing_signature"}
    expected = sign_receipt(_signable_payload(receipt))["signature"]
    return {
        "valid": hmac.compare_digest(signature, expected),
        "reason": "verified" if hmac.compare_digest(signature, expected) else "signature_mismatch",
        "issuer_key_id": receipt.get("issuer_key_id"),
    }
