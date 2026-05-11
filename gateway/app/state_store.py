"""Durable runtime state storage for identity anchors and verification records."""
from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
from datetime import datetime, timezone
from typing import Dict, Iterator, Tuple

from config import settings
from app.models import IdentityData, VerificationStatus


STATE_FILE_NAME = "gateway-state.json"
AUDIT_FILE_NAME = "gateway-audit-log.jsonl"
STATE_FILE_VERSION = 1


def _state_file_path() -> Path:
    return Path(settings.data_dir).expanduser() / STATE_FILE_NAME


def _audit_file_path() -> Path:
    return Path(settings.data_dir).expanduser() / AUDIT_FILE_NAME


def _trust_store_backend() -> str:
    return (settings.trust_store_backend or "local_file").strip().lower()


@contextmanager
def _postgres_connection() -> Iterator[object]:
    if not settings.database_url:
        raise RuntimeError("DATABASE_URL is required for the PostgreSQL trust store.")
    try:
        import psycopg  # type: ignore[import-not-found]
    except ImportError as exc:
        raise RuntimeError(
            "TRUST_STORE_BACKEND=postgres requires the optional psycopg package."
        ) from exc

    with psycopg.connect(settings.database_url) as connection:
        yield connection


def _execute_json(connection: object, sql: str, params: tuple = ()) -> object:
    return connection.execute(sql, params)  # type: ignore[attr-defined]


def _ensure_postgres_schema(connection: object) -> None:
    statements = [
        """
        CREATE TABLE IF NOT EXISTS identity_anchors (
            wallet_address TEXT PRIMARY KEY,
            payload JSONB NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS verification_workflows (
            verification_id TEXT PRIMARY KEY,
            wallet_address TEXT NOT NULL,
            payload JSONB NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS document_evidence_references (
            verification_id TEXT PRIMARY KEY,
            wallet_address TEXT NOT NULL,
            source_sha256 TEXT,
            submitted_hash TEXT,
            content_type TEXT,
            size_bytes INTEGER,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS consent_records (
            verification_id TEXT PRIMARY KEY,
            wallet_address TEXT NOT NULL,
            scope TEXT NOT NULL,
            purpose TEXT NOT NULL,
            status TEXT NOT NULL,
            consent_reference TEXT,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS review_records (
            verification_id TEXT PRIMARY KEY,
            wallet_address TEXT NOT NULL,
            status TEXT NOT NULL,
            reviewer_id TEXT,
            reason TEXT,
            appeal_reference TEXT,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS decision_records (
            verification_id TEXT PRIMARY KEY,
            wallet_address TEXT NOT NULL,
            decision TEXT NOT NULL,
            reason TEXT NOT NULL,
            evidence_status TEXT NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS revocation_records (
            verification_id TEXT PRIMARY KEY,
            wallet_address TEXT NOT NULL,
            status TEXT NOT NULL,
            reason TEXT,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS attestation_records (
            verification_id TEXT PRIMARY KEY,
            wallet_address TEXT NOT NULL,
            status TEXT NOT NULL,
            credential_type TEXT NOT NULL,
            reference TEXT,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS agent_tool_provenance_records (
            verification_id TEXT NOT NULL,
            agent_id TEXT NOT NULL,
            stage TEXT NOT NULL,
            payload JSONB NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (verification_id, stage)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS audit_receipts (
            id TEXT PRIMARY KEY,
            action TEXT NOT NULL,
            wallet_address TEXT NOT NULL,
            target_id TEXT NOT NULL,
            target_type TEXT NOT NULL,
            details TEXT NOT NULL,
            payload JSONB NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """,
    ]
    for statement in statements:
        _execute_json(connection, statement)


def _load_postgres_state() -> Tuple[Dict[str, IdentityData], Dict[str, VerificationStatus]]:
    with _postgres_connection() as connection:
        _ensure_postgres_schema(connection)
        identity_rows = _execute_json(
            connection,
            "SELECT wallet_address, payload FROM identity_anchors",
        ).fetchall()
        verification_rows = _execute_json(
            connection,
            "SELECT verification_id, payload FROM verification_workflows",
        ).fetchall()

    identities = {
        wallet_address: IdentityData.model_validate(payload)
        for wallet_address, payload in identity_rows
    }
    verification_records = {
        verification_id: VerificationStatus.model_validate(payload)
        for verification_id, payload in verification_rows
    }
    return identities, verification_records


def load_gateway_state() -> Tuple[Dict[str, IdentityData], Dict[str, VerificationStatus]]:
    """Load persisted gateway state from the configured trust store."""
    if _trust_store_backend() == "postgres":
        return _load_postgres_state()
    if _trust_store_backend() != "local_file":
        raise NotImplementedError(
            f"Unsupported AadhaarChain trust store backend: {settings.trust_store_backend}"
        )

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
    """Persist gateway runtime state to the configured trust store."""
    if _trust_store_backend() == "postgres":
        _save_postgres_state(identities, verification_records)
        return
    if _trust_store_backend() != "local_file":
        raise NotImplementedError(
            f"Unsupported AadhaarChain trust store backend: {settings.trust_store_backend}"
        )

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


def _save_postgres_state(
    identities: Dict[str, IdentityData],
    verification_records: Dict[str, VerificationStatus],
) -> None:
    with _postgres_connection() as connection:
        _ensure_postgres_schema(connection)
        for wallet_address, identity in identities.items():
            _execute_json(
                connection,
                """
                INSERT INTO identity_anchors (wallet_address, payload, updated_at)
                VALUES (%s, %s::jsonb, now())
                ON CONFLICT (wallet_address)
                DO UPDATE SET payload = EXCLUDED.payload, updated_at = now()
                """,
                (wallet_address, json.dumps(identity.model_dump(mode="json"))),
            )

        for verification_id, status in verification_records.items():
            payload = status.model_dump(mode="json")
            _execute_json(
                connection,
                """
                INSERT INTO verification_workflows (verification_id, wallet_address, payload, updated_at)
                VALUES (%s, %s, %s::jsonb, now())
                ON CONFLICT (verification_id)
                DO UPDATE SET wallet_address = EXCLUDED.wallet_address, payload = EXCLUDED.payload, updated_at = now()
                """,
                (verification_id, status.wallet_address, json.dumps(payload)),
            )
            _sync_derived_postgres_records(connection, status)


def _sync_derived_postgres_records(connection: object, status: VerificationStatus) -> None:
    metadata = status.metadata
    document_type = "aadhaar" if status.verification_id.startswith("aadhaar_") else "pan"
    if metadata is None:
        return

    document_source = metadata.document.source
    _execute_json(
        connection,
        """
        INSERT INTO document_evidence_references
            (verification_id, wallet_address, source_sha256, submitted_hash, content_type, size_bytes, updated_at)
        VALUES (%s, %s, %s, %s, %s, %s, now())
        ON CONFLICT (verification_id) DO UPDATE SET
            source_sha256 = EXCLUDED.source_sha256,
            submitted_hash = EXCLUDED.submitted_hash,
            content_type = EXCLUDED.content_type,
            size_bytes = EXCLUDED.size_bytes,
            updated_at = now()
        """,
        (
            status.verification_id,
            status.wallet_address,
            document_source.sha256 if document_source else None,
            document_source.submitted_hash if document_source else None,
            document_source.content_type if document_source else None,
            document_source.size_bytes if document_source else None,
        ),
    )
    consent_provided = bool(metadata.document.submitted_claims.get("consent_provided", False))
    consent_status = "granted" if document_type == "aadhaar" and consent_provided else "not_required"
    if document_type == "aadhaar" and not consent_provided:
        consent_status = "missing"
    _execute_json(
        connection,
        """
        INSERT INTO consent_records
            (verification_id, wallet_address, scope, purpose, status, consent_reference, updated_at)
        VALUES (%s, %s, %s, %s, %s, %s, now())
        ON CONFLICT (verification_id) DO UPDATE SET
            status = EXCLUDED.status,
            consent_reference = EXCLUDED.consent_reference,
            updated_at = now()
        """,
        (
            status.verification_id,
            status.wallet_address,
            "aadhaar_identity_verification" if document_type == "aadhaar" else "pan_identity_verification",
            "identity_verification",
            consent_status,
            f"consent:{status.verification_id}" if consent_status == "granted" else None,
        ),
    )
    _execute_json(
        connection,
        """
        INSERT INTO decision_records
            (verification_id, wallet_address, decision, reason, evidence_status, updated_at)
        VALUES (%s, %s, %s, %s, %s, now())
        ON CONFLICT (verification_id) DO UPDATE SET
            decision = EXCLUDED.decision,
            reason = EXCLUDED.reason,
            evidence_status = EXCLUDED.evidence_status,
            updated_at = now()
        """,
        (
            status.verification_id,
            status.wallet_address,
            metadata.decision,
            metadata.reason,
            metadata.evidence_status,
        ),
    )
    review_status = {
        "verified": "approved",
        "failed": "rejected",
        "manual_review": "manual_review_required",
    }.get(status.status, "pending")
    _execute_json(
        connection,
        """
        INSERT INTO review_records
            (verification_id, wallet_address, status, reason, updated_at)
        VALUES (%s, %s, %s, %s, now())
        ON CONFLICT (verification_id) DO UPDATE SET
            status = EXCLUDED.status,
            reason = EXCLUDED.reason,
            updated_at = now()
        """,
        (status.verification_id, status.wallet_address, review_status, status.error or metadata.reason),
    )
    revocation_status = "active" if status.status == "failed" else "not_applicable"
    _execute_json(
        connection,
        """
        INSERT INTO revocation_records
            (verification_id, wallet_address, status, reason, updated_at)
        VALUES (%s, %s, %s, %s, now())
        ON CONFLICT (verification_id) DO UPDATE SET
            status = EXCLUDED.status,
            reason = EXCLUDED.reason,
            updated_at = now()
        """,
        (status.verification_id, status.wallet_address, revocation_status, status.error),
    )
    _execute_json(
        connection,
        """
        INSERT INTO attestation_records
            (verification_id, wallet_address, status, credential_type, reference, updated_at)
        VALUES (%s, %s, %s, %s, %s, now())
        ON CONFLICT (verification_id) DO UPDATE SET
            status = EXCLUDED.status,
            credential_type = EXCLUDED.credential_type,
            reference = EXCLUDED.reference,
            updated_at = now()
        """,
        (status.verification_id, status.wallet_address, "not_issued", document_type, None),
    )
    for stage, provenance in {
        "document": metadata.document.provenance,
        "fraud": metadata.fraud.provenance,
        "compliance": metadata.compliance.provenance,
    }.items():
        _execute_json(
            connection,
            """
            INSERT INTO agent_tool_provenance_records
                (verification_id, agent_id, stage, payload, updated_at)
            VALUES (%s, %s, %s, %s::jsonb, now())
            ON CONFLICT (verification_id, stage) DO UPDATE SET
                agent_id = EXCLUDED.agent_id,
                payload = EXCLUDED.payload,
                updated_at = now()
            """,
            (
                status.verification_id,
                provenance.agent_id,
                stage,
                json.dumps(provenance.model_dump(mode="json")),
            ),
        )


def append_audit_event(
    action: str,
    wallet_address: str,
    *,
    target_id: str,
    target_type: str,
    details: str,
) -> dict:
    """Append an immutable local audit event for demo/fixture trust operations."""
    if _trust_store_backend() == "postgres":
        with _postgres_connection() as connection:
            _ensure_postgres_schema(connection)
            event = _build_audit_event(action, wallet_address, target_id, target_type, details)
            _execute_json(
                connection,
                """
                INSERT INTO audit_receipts
                    (id, action, wallet_address, target_id, target_type, details, payload, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s)
                """,
                (
                    event["id"],
                    action,
                    wallet_address,
                    target_id,
                    target_type,
                    details,
                    json.dumps(event),
                    event["created_at"],
                ),
            )
            return event
    if _trust_store_backend() != "local_file":
        raise NotImplementedError(
            f"Unsupported AadhaarChain trust store backend: {settings.trust_store_backend}"
        )

    path = _audit_file_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    event = _build_audit_event(action, wallet_address, target_id, target_type, details)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, sort_keys=True))
        handle.write("\n")
    return event


def _build_audit_event(
    action: str,
    wallet_address: str,
    target_id: str,
    target_type: str,
    details: str,
) -> dict:
    now = datetime.now(timezone.utc)
    return {
        "id": f"audit-{now.timestamp():.6f}",
        "action": action,
        "wallet_address": wallet_address,
        "target_id": target_id,
        "target_type": target_type,
        "details": details,
        "created_at": now.isoformat(),
    }


def load_audit_events() -> list[dict]:
    """Load immutable local audit events."""
    if _trust_store_backend() == "postgres":
        with _postgres_connection() as connection:
            _ensure_postgres_schema(connection)
            rows = _execute_json(
                connection,
                "SELECT payload FROM audit_receipts ORDER BY created_at ASC",
            ).fetchall()
        return [row[0] for row in rows]
    if _trust_store_backend() != "local_file":
        raise NotImplementedError(
            f"Unsupported AadhaarChain trust store backend: {settings.trust_store_backend}"
        )

    path = _audit_file_path()
    if not path.exists():
        return []

    events: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        events.append(json.loads(line))
    return events
