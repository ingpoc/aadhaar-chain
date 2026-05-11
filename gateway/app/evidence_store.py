"""Encrypted private evidence storage for AadhaarChain verification uploads."""
from __future__ import annotations

import base64
import json
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from config import settings
from app.models import DocumentEvidenceSource


EVIDENCE_STORE_VERSION = 1
EVIDENCE_ALGORITHM = "fernet-aes128-cbc-hmac-sha256"


def _evidence_dir() -> Path:
    return Path(settings.data_dir).expanduser() / "encrypted-evidence"


def _fernet() -> Fernet:
    key = settings.evidence_encryption_key
    if not key:
        raise RuntimeError("EVIDENCE_ENCRYPTION_KEY is required to store private evidence.")
    try:
        decoded = base64.urlsafe_b64decode(key)
    except Exception as exc:
        raise RuntimeError("EVIDENCE_ENCRYPTION_KEY must be a urlsafe base64 Fernet key.") from exc
    if len(decoded) != 32:
        raise RuntimeError("EVIDENCE_ENCRYPTION_KEY must decode to 32 bytes.")
    return Fernet(key.encode("ascii"))


def store_encrypted_evidence(
    *,
    verification_id: str,
    wallet_address: str,
    document_data: bytes,
    source: DocumentEvidenceSource,
) -> dict:
    """Store uploaded document bytes as ciphertext and return a safe reference."""
    if not source.sha256:
        raise RuntimeError("Evidence source hash is required before encrypted storage.")

    store_dir = _evidence_dir()
    store_dir.mkdir(parents=True, exist_ok=True)
    path = store_dir / f"{verification_id}.json"
    ciphertext = _fernet().encrypt(document_data).decode("ascii")
    payload = {
        "version": EVIDENCE_STORE_VERSION,
        "algorithm": EVIDENCE_ALGORITHM,
        "key_id": settings.evidence_encryption_key_id,
        "verification_id": verification_id,
        "wallet_address": wallet_address,
        "sha256": source.sha256,
        "content_type": source.content_type,
        "size_bytes": source.size_bytes,
        "ciphertext": ciphertext,
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return {
        "reference": f"evidence:{verification_id}",
        "path": str(path),
        "sha256": source.sha256,
        "key_id": settings.evidence_encryption_key_id,
        "algorithm": EVIDENCE_ALGORITHM,
    }


def load_encrypted_evidence(reference: str) -> bytes:
    """Load and decrypt private evidence by internal reference."""
    if not reference.startswith("evidence:"):
        raise ValueError("Evidence reference must use evidence:{verification_id}.")
    verification_id = reference.split(":", 1)[1]
    path = _evidence_dir() / f"{verification_id}.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    try:
        return _fernet().decrypt(payload["ciphertext"].encode("ascii"))
    except InvalidToken as exc:
        raise RuntimeError("Evidence ciphertext could not be decrypted with the configured key.") from exc
