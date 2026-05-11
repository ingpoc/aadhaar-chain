import os
import sys

from fastapi.testclient import TestClient

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from config import get_runtime_mode, settings, validate_runtime_storage_config
from main import app
from cryptography.fernet import Fernet
from app.models import (
    AgentRunProvenance,
    ComplianceVerificationEvidence,
    DocumentVerificationEvidence,
    FraudVerificationEvidence,
    IdentityData,
    VerificationMetadata,
    VerificationStatus,
)
from app.routes import agent_manager, identities, verification_rate_buckets
from app.state_store import load_audit_events, load_gateway_state, save_gateway_state
from app.evidence_store import load_encrypted_evidence


def _provenance(agent_id: str) -> AgentRunProvenance:
    return AgentRunProvenance(
        agent_id=agent_id,
        status="completed",
        started_at="2026-03-17T00:00:00Z",
        completed_at="2026-03-17T00:00:01Z",
        tools=[],
    )


def test_create_aadhaar_verification_accepts_multipart_upload(monkeypatch) -> None:
    captured: dict[str, object] = {}

    async def fake_create_verification(wallet_address: str, document_type: str, verification_data):
        captured["create_wallet_address"] = wallet_address
        captured["create_document_type"] = document_type
        captured["create_verification_data"] = verification_data
        return f"{document_type}_{wallet_address}"

    async def fake_orchestrate(wallet_address: str, document_type: str, document_data: bytes, verification_data, document_source):
        captured["orchestrate_wallet_address"] = wallet_address
        captured["orchestrate_document_type"] = document_type
        captured["document_data"] = document_data
        captured["verification_data"] = verification_data
        captured["document_source"] = document_source
        return None

    monkeypatch.setattr(agent_manager, "create_verification", fake_create_verification)
    monkeypatch.setattr(agent_manager, "orchestrate_verification", fake_orchestrate)

    client = TestClient(app)
    response = client.post(
        "/api/identity/wallet123/aadhaar",
        data={
            "name": "Alice Example",
            "dob": "1990-01-01",
            "uid": "123456789012",
            "consent_provided": "true",
        },
        files={
            "document": ("aadhaar.pdf", b"%PDF-1.4 test document", "application/pdf"),
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["data"]["verification_id"] == "aadhaar_wallet123"

    assert captured["create_wallet_address"] == "wallet123"
    assert captured["create_document_type"] == "aadhaar"
    assert captured["orchestrate_wallet_address"] == "wallet123"
    assert captured["orchestrate_document_type"] == "aadhaar"
    assert captured["document_data"] == b"%PDF-1.4 test document"

    verification_data = captured["verification_data"]
    assert getattr(verification_data, "uid") == "123456789012"
    assert getattr(verification_data, "consent_provided") is True

    document_source = captured["document_source"]
    assert getattr(document_source, "transport") == "upload"
    assert getattr(document_source, "file_name") == "aadhaar.pdf"
    assert getattr(document_source, "content_type") == "application/pdf"
    assert getattr(document_source, "size_bytes") == len(b"%PDF-1.4 test document")
    assert getattr(document_source, "sha256").startswith("sha256:")


def test_get_identity_returns_empty_payload_when_missing() -> None:
    identities.clear()

    client = TestClient(app)
    response = client.get("/api/identity/missing-wallet")

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["message"] == "Identity not found"
    assert body["data"] is None


def test_get_trust_surface_returns_no_identity_contract_when_missing() -> None:
    identities.clear()
    agent_manager.verification_records.clear()

    client = TestClient(app)
    response = client.get("/api/identity/missing-wallet/trust")

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    trust = body["data"]
    assert trust["wallet_address"] == "missing-wallet"
    assert trust["trust_state"] == "no_identity"
    assert trust["high_trust_eligible"] is False
    assert trust["verifications"] == []


def test_get_trust_surface_openapi_schema_locks_downstream_contract() -> None:
    schema = app.openapi()
    route_schema = schema["paths"]["/api/identity/{wallet_address}/trust"]["get"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"]
    assert route_schema == {"$ref": "#/components/schemas/TrustReadSurfaceResponse"}

    components = schema["components"]["schemas"]
    trust_response = components["TrustReadSurfaceResponse"]
    assert trust_response["properties"]["data"] == {"$ref": "#/components/schemas/TrustReadSurface"}
    assert "data" in trust_response["required"]

    trust_surface = components["TrustReadSurface"]
    expected_properties = {
        "trust_version",
        "wallet_address",
        "did",
        "verification_bitmap",
        "updated_at",
        "trust_state",
        "high_trust_eligible",
        "state_reason",
        "verifications",
    }
    assert set(trust_surface["properties"]) == expected_properties
    assert trust_surface["properties"]["trust_state"]["enum"] == [
        "no_identity",
        "identity_present_unverified",
        "verified",
        "manual_review",
        "revoked_or_blocked",
    ]
    assert trust_surface["properties"]["trust_version"]["const"] == "v1"


def test_create_identity_persists_runtime_state(tmp_path) -> None:
    identities.clear()
    agent_manager.verification_records.clear()

    original_data_dir = settings.data_dir
    settings.data_dir = str(tmp_path)
    try:
        client = TestClient(app)
        response = client.post(
            "/api/identity/wallet-persisted",
            json={"commitment": "sha256:persisted"},
        )

        assert response.status_code == 200
        assert response.json()["success"] is True

        identities.clear()
        agent_manager.verification_records.clear()
        persisted_identities, persisted_verifications = load_gateway_state()

        assert "wallet-persisted" in persisted_identities
        assert persisted_identities["wallet-persisted"].commitment == "sha256:persisted"
        assert persisted_verifications == {}
        audit_events = load_audit_events()
        assert audit_events[-1]["action"] == "identity_created"
        assert audit_events[-1]["wallet_address"] == "wallet-persisted"
    finally:
        settings.data_dir = original_data_dir


def test_trust_surface_reads_append_audit_receipts(tmp_path) -> None:
    identities.clear()
    agent_manager.verification_records.clear()

    original_data_dir = settings.data_dir
    settings.data_dir = str(tmp_path)
    try:
        client = TestClient(app)
        response = client.get("/api/identity/missing-wallet/trust")

        assert response.status_code == 200
        audit_events = load_audit_events()
        assert audit_events[-1]["action"] == "trust_read"
        assert audit_events[-1]["wallet_address"] == "missing-wallet"
        assert audit_events[-1]["details"] == "Downstream trust read returned no identity."
    finally:
        settings.data_dir = original_data_dir


def test_runtime_mode_normalizes_production_alias() -> None:
    original_env = settings.aadhaar_chain_env
    try:
        settings.aadhaar_chain_env = "prod"
        assert get_runtime_mode() == "production"
        settings.aadhaar_chain_env = "staging"
        assert get_runtime_mode() == "staging"
        settings.aadhaar_chain_env = "local"
        assert get_runtime_mode() == "demo"
    finally:
        settings.aadhaar_chain_env = original_env


def test_production_runtime_rejects_local_file_trust_store() -> None:
    original_env = settings.aadhaar_chain_env
    original_backend = settings.trust_store_backend
    original_database_url = settings.database_url
    try:
        settings.aadhaar_chain_env = "production"
        settings.trust_store_backend = "local_file"
        settings.database_url = None

        try:
            validate_runtime_storage_config()
        except RuntimeError as exc:
            assert "TRUST_STORE_BACKEND=postgres" in str(exc)
        else:
            raise AssertionError("production local_file trust store should be rejected")
    finally:
        settings.aadhaar_chain_env = original_env
        settings.trust_store_backend = original_backend
        settings.database_url = original_database_url


def test_production_runtime_accepts_postgres_store_config() -> None:
    original_env = settings.aadhaar_chain_env
    original_backend = settings.trust_store_backend
    original_database_url = settings.database_url
    original_key = settings.evidence_encryption_key
    try:
        settings.aadhaar_chain_env = "production"
        settings.trust_store_backend = "postgres"
        settings.database_url = "postgresql://example/identity"
        settings.evidence_encryption_key = Fernet.generate_key().decode("ascii")

        validate_runtime_storage_config()
    finally:
        settings.aadhaar_chain_env = original_env
        settings.trust_store_backend = original_backend
        settings.database_url = original_database_url
        settings.evidence_encryption_key = original_key


def test_production_runtime_requires_evidence_encryption_key() -> None:
    original_env = settings.aadhaar_chain_env
    original_backend = settings.trust_store_backend
    original_database_url = settings.database_url
    original_key = settings.evidence_encryption_key
    try:
        settings.aadhaar_chain_env = "production"
        settings.trust_store_backend = "postgres"
        settings.database_url = "postgresql://example/identity"
        settings.evidence_encryption_key = None

        try:
            validate_runtime_storage_config()
        except RuntimeError as exc:
            assert "EVIDENCE_ENCRYPTION_KEY" in str(exc)
        else:
            raise AssertionError("production must require encrypted evidence storage")
    finally:
        settings.aadhaar_chain_env = original_env
        settings.trust_store_backend = original_backend
        settings.database_url = original_database_url
        settings.evidence_encryption_key = original_key


def test_save_gateway_state_round_trips_verifications(tmp_path) -> None:
    original_data_dir = settings.data_dir
    settings.data_dir = str(tmp_path)
    try:
        identity = IdentityData(
            did="did:solana:wallet123",
            owner="wallet123",
            commitment="sha256:anchor",
            verification_bitmap=0,
            created_at="2026-03-17T00:00:00Z",
            updated_at="2026-03-17T00:00:00Z",
        )
        verification = VerificationStatus(
            verification_id="aadhaar_wallet123",
            wallet_address="wallet123",
            status="manual_review",
            current_step="complete",
            steps=[],
            progress=1.0,
            created_at="2026-03-17T00:00:00Z",
            updated_at="2026-03-17T00:00:01Z",
            error="manual review required",
            metadata=None,
        )

        save_gateway_state(
            {"wallet123": identity},
            {"aadhaar_wallet123": verification},
        )

        persisted_identities, persisted_verifications = load_gateway_state()

        assert persisted_identities["wallet123"].owner == "wallet123"
        assert persisted_verifications["aadhaar_wallet123"].status == "manual_review"
        assert persisted_verifications["aadhaar_wallet123"].wallet_address == "wallet123"
    finally:
        settings.data_dir = original_data_dir


def test_get_trust_surface_redacts_internal_verification_evidence() -> None:
    identities.clear()
    agent_manager.verification_records.clear()

    wallet_address = "wallet123"
    identities[wallet_address] = IdentityData(
        did=f"did:solana:{wallet_address}",
        owner=wallet_address,
        commitment="sha256:test",
        verification_bitmap=0,
        created_at="2026-03-17T00:00:00Z",
        updated_at="2026-03-17T00:00:00Z",
    )
    agent_manager.verification_records["aadhaar_wallet123"] = VerificationStatus(
        verification_id="aadhaar_wallet123",
        wallet_address=wallet_address,
        status="manual_review",
        current_step="complete",
        steps=[],
        progress=1.0,
        created_at="2026-03-17T00:00:00Z",
        updated_at="2026-03-17T00:01:00Z",
        error="Fraud analysis requested manual review.",
        metadata=VerificationMetadata(
            decision="manual_review",
            reason="Fraud analysis requested manual review.",
            evidence_status="complete",
            document=DocumentVerificationEvidence(
                document_type="aadhaar",
                input_kind="raw_document",
                extracted_fields={"name": "Alice Example", "uid": "123456789012"},
                submitted_claims={"consent_provided": True, "uid": "123456789012"},
                confidence=0.9,
                warnings=[],
                required_fields=["name", "dob", "uid"],
                missing_fields=[],
                provenance=_provenance("document-validator"),
                gaps=[],
            ),
            fraud=FraudVerificationEvidence(
                risk_score=0.61,
                risk_level="medium",
                indicators=["Name mismatch"],
                recommendation="manual_review",
                provenance=_provenance("fraud-detection"),
                gaps=[],
            ),
            compliance=ComplianceVerificationEvidence(
                aadhaar_act_compliant=True,
                dpdp_compliant=True,
                violations=[],
                recommendation="approve",
                provenance=_provenance("compliance-monitor"),
                gaps=[],
            ),
            blocking_gaps=[],
            assumptions=[],
        ),
    )

    client = TestClient(app)
    response = client.get(f"/api/identity/{wallet_address}/trust")

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    trust = body["data"]
    assert trust["trust_version"] == "v1"
    assert trust["wallet_address"] == wallet_address
    assert trust["trust_state"] == "manual_review"
    assert trust["high_trust_eligible"] is False
    assert trust["state_reason"] == "Fraud analysis requested manual review."
    assert trust["verifications"][0]["document_type"] == "aadhaar"
    assert trust["verifications"][0]["workflow_status"] == "manual_review"
    assert trust["verifications"][0]["evidence_status"] == "complete"
    assert trust["verifications"][0]["consent"]["status"] == "granted"
    assert trust["verifications"][0]["review"]["status"] == "manual_review_required"
    assert "document" not in trust["verifications"][0]
    assert "fraud" not in trust["verifications"][0]
    assert "compliance" not in trust["verifications"][0]


def test_get_trust_surface_defaults_to_identity_present_unverified_without_verifications() -> None:
    identities.clear()
    agent_manager.verification_records.clear()

    wallet_address = "wallet456"
    identities[wallet_address] = IdentityData(
        did=f"did:solana:{wallet_address}",
        owner=wallet_address,
        commitment="sha256:anchor",
        verification_bitmap=0,
        created_at="2026-03-17T00:00:00Z",
        updated_at="2026-03-17T00:00:00Z",
    )

    client = TestClient(app)
    response = client.get(f"/api/identity/{wallet_address}/trust")

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    trust = body["data"]
    assert trust["trust_state"] == "identity_present_unverified"
    assert trust["high_trust_eligible"] is False
    assert trust["state_reason"] == "Identity anchor exists, but no approved verification is available yet."
    assert trust["verifications"] == []


def test_seed_trust_fixture_supports_full_local_matrix() -> None:
    identities.clear()
    agent_manager.verification_records.clear()

    client = TestClient(app)
    wallet_address = "wallet-fixture"

    expected_states = {
        "identity_present_unverified": "identity_present_unverified",
        "verified": "verified",
        "manual_review": "manual_review",
        "revoked_or_blocked": "revoked_or_blocked",
    }

    for fixture_state, expected_state in expected_states.items():
        response = client.post(
            f"/api/identity/dev/fixtures/{wallet_address}",
            json={"fixture_state": fixture_state, "document_type": "aadhaar"},
        )

        assert response.status_code == 200
        body = response.json()
        assert body["success"] is True
        assert body["data"]["trust_state"] == expected_state

    response = client.post(
        f"/api/identity/dev/fixtures/{wallet_address}",
        json={"fixture_state": "no_identity", "document_type": "aadhaar"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["data"]["trust_state"] == "no_identity"
    assert body["data"]["high_trust_eligible"] is False

    identity_response = client.get(f"/api/identity/{wallet_address}")
    assert identity_response.status_code == 200
    assert identity_response.json()["data"] is None


def test_aadhaar_upload_stores_encrypted_evidence_when_key_is_configured(monkeypatch, tmp_path) -> None:
    original_data_dir = settings.data_dir
    original_key = settings.evidence_encryption_key
    settings.data_dir = str(tmp_path)
    settings.evidence_encryption_key = Fernet.generate_key().decode("ascii")
    identities.clear()
    agent_manager.verification_records.clear()
    captured: dict[str, object] = {}

    async def fake_create_verification(wallet_address: str, document_type: str, verification_data):
        del verification_data
        return f"{document_type}_{wallet_address}"

    async def fake_orchestrate(wallet_address: str, document_type: str, document_data: bytes, verification_data, document_source):
        captured["document_data"] = document_data
        captured["document_source"] = document_source
        captured["verification_data"] = verification_data

    monkeypatch.setattr(agent_manager, "create_verification", fake_create_verification)
    monkeypatch.setattr(agent_manager, "orchestrate_verification", fake_orchestrate)
    try:
        client = TestClient(app)
        response = client.post(
            "/api/identity/wallet-evidence/aadhaar",
            data={
                "name": "Alice Example",
                "dob": "1990-01-01",
                "uid": "123456789012",
                "consent_provided": "true",
            },
            files={
                "document": ("aadhaar.pdf", b"private aadhaar bytes", "application/pdf"),
            },
        )
        assert response.status_code == 200

        encrypted_file = tmp_path / "encrypted-evidence" / "aadhaar_wallet-evidence.json"
        assert encrypted_file.exists()
        encrypted_payload = encrypted_file.read_text(encoding="utf-8")
        assert "private aadhaar bytes" not in encrypted_payload
        assert load_encrypted_evidence("evidence:aadhaar_wallet-evidence") == b"private aadhaar bytes"

        audit_actions = [event["action"] for event in load_audit_events()]
        assert "evidence_stored" in audit_actions
    finally:
        settings.data_dir = original_data_dir
        settings.evidence_encryption_key = original_key
        identities.clear()
        agent_manager.verification_records.clear()


def test_verification_upload_rate_limit_is_enforced(monkeypatch) -> None:
    original_limit = settings.verification_rate_limit_per_minute
    original_key = settings.evidence_encryption_key
    settings.verification_rate_limit_per_minute = 1
    settings.evidence_encryption_key = None
    verification_rate_buckets.clear()

    async def fake_create_verification(wallet_address: str, document_type: str, verification_data):
        del verification_data
        return f"{document_type}_{wallet_address}"

    async def fake_orchestrate(wallet_address: str, document_type: str, document_data: bytes, verification_data, document_source):
        del wallet_address, document_type, document_data, verification_data, document_source

    monkeypatch.setattr(agent_manager, "create_verification", fake_create_verification)
    monkeypatch.setattr(agent_manager, "orchestrate_verification", fake_orchestrate)
    try:
        client = TestClient(app)
        payload = {
            "data": {
                "name": "Alice Example",
                "dob": "1990-01-01",
                "uid": "123456789012",
                "consent_provided": "true",
            },
            "files": {
                "document": ("aadhaar.pdf", b"private aadhaar bytes", "application/pdf"),
            },
        }
        first = client.post("/api/identity/wallet-rate/aadhaar", **payload)
        second = client.post("/api/identity/wallet-rate/aadhaar", **payload)

        assert first.status_code == 200
        assert second.status_code == 429
    finally:
        settings.verification_rate_limit_per_minute = original_limit
        settings.evidence_encryption_key = original_key
        verification_rate_buckets.clear()


def test_review_queue_evidence_access_and_decision_are_audited(tmp_path) -> None:
    original_data_dir = settings.data_dir
    settings.data_dir = str(tmp_path)
    identities.clear()
    agent_manager.verification_records.clear()
    try:
        client = TestClient(app)
        wallet_address = "wallet-review"
        verification_id = f"aadhaar_{wallet_address}_fixture"

        fixture_response = client.post(
            f"/api/identity/dev/fixtures/{wallet_address}",
            json={"fixture_state": "manual_review", "document_type": "aadhaar"},
        )
        assert fixture_response.status_code == 200

        queue_response = client.get("/api/identity/reviews/queue")
        assert queue_response.status_code == 200
        queue_items = queue_response.json()["data"]["items"]
        assert [item["verification_id"] for item in queue_items] == [verification_id]
        assert queue_items[0]["review"]["status"] == "manual_review_required"

        access_response = client.post(
            f"/api/identity/reviews/{verification_id}/evidence-access",
            json={"reviewer_id": "reviewer-1", "purpose": "manual_review"},
        )
        assert access_response.status_code == 200
        access_payload = access_response.json()["data"]
        assert access_payload["raw_evidence_returned"] is False
        assert "extracted_fields" not in access_payload
        assert "submitted_claims" not in access_payload

        decision_response = client.post(
            f"/api/identity/reviews/{verification_id}/decision",
            json={
                "reviewer_id": "reviewer-1",
                "decision": "approve",
                "reason": "Reviewer accepted the evidence completeness record.",
            },
        )
        assert decision_response.status_code == 200
        trust = decision_response.json()["data"]["trust"]
        assert trust["trust_state"] == "verified"
        assert trust["high_trust_eligible"] is True
        receipt_kinds = {
            receipt["kind"]
            for receipt in trust["verifications"][0]["audit_receipts"]
        }
        assert "review_record" in receipt_kinds

        audit_actions = [event["action"] for event in load_audit_events()]
        assert "evidence_accessed" in audit_actions
        assert "review_decision_recorded" in audit_actions
    finally:
        settings.data_dir = original_data_dir
        identities.clear()
        agent_manager.verification_records.clear()


def test_trust_revocation_updates_trust_surface_and_audit(tmp_path) -> None:
    original_data_dir = settings.data_dir
    settings.data_dir = str(tmp_path)
    identities.clear()
    agent_manager.verification_records.clear()
    try:
        client = TestClient(app)
        wallet_address = "wallet-revoked"
        verification_id = f"aadhaar_{wallet_address}_fixture"

        fixture_response = client.post(
            f"/api/identity/dev/fixtures/{wallet_address}",
            json={"fixture_state": "verified", "document_type": "aadhaar"},
        )
        assert fixture_response.status_code == 200

        revoke_response = client.post(
            f"/api/identity/{wallet_address}/trust/{verification_id}/revoke",
            json={"operator_id": "ops-1", "reason": "Subject requested revocation."},
        )
        assert revoke_response.status_code == 200
        trust = revoke_response.json()["data"]["trust"]
        assert trust["trust_state"] == "revoked_or_blocked"
        verification = trust["verifications"][0]
        assert verification["revocation"]["status"] == "active"
        assert "revocation_record" in {
            receipt["kind"] for receipt in verification["audit_receipts"]
        }

        audit_actions = [event["action"] for event in load_audit_events()]
        assert "trust_revoked" in audit_actions
    finally:
        settings.data_dir = original_data_dir
        identities.clear()
        agent_manager.verification_records.clear()
