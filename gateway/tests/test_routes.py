import os
import sys

from fastapi.testclient import TestClient

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from main import app
from app.models import (
    AgentRunProvenance,
    ComplianceVerificationEvidence,
    DocumentVerificationEvidence,
    FraudVerificationEvidence,
    IdentityData,
    VerificationMetadata,
    VerificationStatus,
)
from app.routes import agent_manager, identities


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
    assert body["data"] is None

    identity_response = client.get(f"/api/identity/{wallet_address}")
    assert identity_response.status_code == 200
    assert identity_response.json()["data"] is None
