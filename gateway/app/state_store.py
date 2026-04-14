"""Durable runtime state storage for identity anchors and verification records."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, Tuple

from config import settings
from app.models import IdentityData, VerificationStatus


STATE_FILE_NAME = "gateway-state.json"
STATE_FILE_VERSION = 1


def _state_file_path() -> Path:
    return Path(settings.data_dir).expanduser() / STATE_FILE_NAME


def load_gateway_state() -> Tuple[Dict[str, IdentityData], Dict[str, VerificationStatus]]:
    """Load persisted gateway state from disk."""
    path = _state_file_path()
    if not path.exists():
        return {}, {}

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Gateway state file is not valid JSON: {path}") from exc

    identities_payload = payload.get("identities", {})
    verifications_payload = payload.get("verification_records", {})

    try:
        identities = {
            wallet_address: IdentityData.model_validate(identity_payload)
            for wallet_address, identity_payload in identities_payload.items()
        }
        verification_records = {
            verification_id: VerificationStatus.model_validate(status_payload)
            for verification_id, status_payload in verifications_payload.items()
        }
    except Exception as exc:
        raise RuntimeError(f"Gateway state file contains invalid records: {path}") from exc

    return identities, verification_records


def save_gateway_state(
    identities: Dict[str, IdentityData],
    verification_records: Dict[str, VerificationStatus],
) -> None:
    """Persist gateway runtime state to disk atomically."""
    path = _state_file_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    payload = {
        "version": STATE_FILE_VERSION,
        "identities": {
            wallet_address: identity.model_dump(mode="json")
            for wallet_address, identity in identities.items()
        },
        "verification_records": {
            verification_id: status.model_dump(mode="json")
            for verification_id, status in verification_records.items()
        },
    }

    temp_path = path.with_suffix(".tmp")
    temp_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    temp_path.replace(path)
