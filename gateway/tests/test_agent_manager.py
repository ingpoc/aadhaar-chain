import asyncio
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.agent_manager import AgentManager
from app.models import (
    AadhaarVerificationData,
    AgentRunProvenance,
    ComplianceVerificationEvidence,
    DocumentVerificationEvidence,
    FraudVerificationEvidence,
)


def _provenance(agent_id: str, status: str = "completed") -> AgentRunProvenance:
    return AgentRunProvenance(
        agent_id=agent_id,
        status=status,  # type: ignore[arg-type]
        started_at="2026-03-17T00:00:00Z",
        completed_at="2026-03-17T00:00:01Z",
        tools=[],
    )


def test_validate_document_marks_request_payload_as_unverified() -> None:
    manager = AgentManager()
    verification = AadhaarVerificationData(
        name="Alice Example",
        dob="1990-01-01",
        uid="123456789012",
        consent_provided=True,
    )

    evidence = asyncio.run(
        manager.validate_document(
            verification.model_dump_json().encode("utf-8"),
            "aadhaar",
            verification,
        )
    )

    assert evidence.input_kind == "request_payload"
    assert evidence.extracted_fields == {}
    assert any(gap.code == "primary_document_missing" for gap in evidence.gaps)


def test_orchestrate_verification_requires_manual_review_without_primary_document() -> None:
    manager = AgentManager()
    verification = AadhaarVerificationData(
        name="Alice Example",
        dob="1990-01-01",
        uid="123456789012",
        consent_provided=True,
    )

    status = asyncio.run(
        manager.orchestrate_verification(
            "wallet123",
            "aadhaar",
            verification.model_dump_json().encode("utf-8"),
            verification,
        )
    )

    assert status.status == "manual_review"
    assert status.metadata is not None
    assert status.metadata.decision == "manual_review"
    assert any(gap.code == "primary_document_missing" for gap in status.metadata.blocking_gaps)


def test_build_document_source_hashes_uploaded_bytes() -> None:
    manager = AgentManager()

    source = manager.build_document_source(
        "upload",
        b"fake-pdf-bytes",
        file_name="aadhaar.pdf",
        content_type="application/pdf",
        submitted_hash="sha256:deadbeef",
    )

    assert source.transport == "upload"
    assert source.file_name == "aadhaar.pdf"
    assert source.content_type == "application/pdf"
    assert source.size_bytes == len(b"fake-pdf-bytes")
    assert source.sha256 is not None
    assert source.submitted_hash == "sha256:deadbeef"
    assert source.hash_matches_submission is False


def test_validate_document_uses_uploaded_source_metadata() -> None:
    manager = AgentManager()
    verification = AadhaarVerificationData(
        name="Alice Example",
        dob="1990-01-01",
        uid="123456789012",
        consent_provided=True,
    )
    source = manager.build_document_source(
        "upload",
        b"%PDF-1.4 test document",
        file_name="aadhaar.pdf",
        content_type="application/pdf",
    )

    async def fake_invoke_agent(*args, **kwargs):
        del args, kwargs
        return (
            {
                "document_type": "aadhaar",
                "fields": {
                    "name": "Alice Example",
                    "dob": "1990-01-01",
                    "uid": "123456789012",
                },
                "confidence": 0.96,
                "warnings": [],
            },
            _provenance("document-validator"),
        )

    manager.invoke_agent = fake_invoke_agent  # type: ignore[method-assign]

    evidence = asyncio.run(
        manager.validate_document(
            b"%PDF-1.4 test document",
            "aadhaar",
            verification,
            source,
        )
    )

    assert evidence.input_kind == "raw_document"
    assert evidence.source is not None
    assert evidence.source.transport == "upload"
    assert evidence.source.file_name == "aadhaar.pdf"
    assert evidence.source.sha256 is not None
    assert all(gap.code != "primary_document_missing" for gap in evidence.gaps)


def test_build_metadata_only_approves_complete_evidence_contract() -> None:
    manager = AgentManager()
    document = DocumentVerificationEvidence(
        document_type="pan",
        input_kind="raw_document",
        extracted_fields={
            "name": "Alice Example",
            "dob": "1990-01-01",
            "pan_number": "ABCDE1234F",
        },
        submitted_claims={},
        confidence=0.96,
        warnings=[],
        required_fields=["name", "dob", "pan_number"],
        missing_fields=[],
        provenance=_provenance("document-validator"),
        gaps=[],
    )
    fraud = FraudVerificationEvidence(
        risk_score=0.08,
        risk_level="safe",
        indicators=[],
        recommendation="approve",
        provenance=_provenance("fraud-detection"),
        gaps=[],
    )
    compliance = ComplianceVerificationEvidence(
        aadhaar_act_compliant=True,
        dpdp_compliant=True,
        violations=[],
        recommendation="approve",
        provenance=_provenance("compliance-monitor"),
        gaps=[],
    )

    metadata = manager._build_metadata("pan", document, fraud, compliance)

    assert metadata.decision == "approve"
    assert metadata.evidence_status == "complete"
    assert metadata.blocking_gaps == []
