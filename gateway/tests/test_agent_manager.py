import asyncio
import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.agent_manager import AgentManager, AgentType
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


def test_build_mcp_servers_normalizes_prefixed_server_names() -> None:
    manager = AgentManager()

    servers = manager._build_mcp_servers(
        ["mcp://document-processor", "pattern-analyzer", "mcp://compliance-rules/check_dpdp"]
    )

    assert "document-processor" in servers
    assert "pattern-analyzer" in servers
    assert "compliance-rules" in servers
    assert all(name.startswith("mcp://") is False for name in servers)


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

    evidence = asyncio.run(
        manager.validate_document(
            b"UIDAI\nAlice Example\n01/01/1990\n1234 5678 9012",
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


def test_validate_document_falls_back_to_deterministic_contract_when_agent_returns_none() -> None:
    manager = AgentManager()
    verification = AadhaarVerificationData(
        name="Alice Example",
        dob="1990-01-01",
        uid="123456789012",
        consent_provided=True,
    )
    source = manager.build_document_source(
        "upload",
        b"UIDAI\nAlice Example\n01/01/1990\n1234 5678 9012",
        file_name="aadhaar.txt",
        content_type="text/plain",
    )

    evidence = asyncio.run(
        manager.validate_document(
            b"UIDAI\nAlice Example\n01/01/1990\n1234 5678 9012",
            "aadhaar",
            verification,
            source,
        )
    )

    assert evidence.provenance.status == "completed"
    assert evidence.provenance.model == "document-processor-local"
    assert evidence.input_kind == "raw_document"
    assert evidence.extracted_fields["uid"] == "123456789012"
    assert evidence.extracted_fields["name"] == "Alice Example"
    assert all(gap.code != "document_text_missing" for gap in evidence.gaps)


def test_validate_document_reports_processor_runtime_failure() -> None:
    manager = AgentManager()
    verification = AadhaarVerificationData(
        name="Alice Example",
        dob="1990-01-01",
        uid="123456789012",
        consent_provided=True,
    )
    source = manager.build_document_source(
        "upload",
        b"fake-image-bytes",
        file_name="aadhaar.png",
        content_type="image/png",
    )

    with patch(
        "app.agent_manager.extract_document_contract",
        return_value=type(
            "DocumentResult",
            (),
            {
                "detected_document_type": "aadhaar",
                "text_method": "ocr_unavailable",
                "fields": {"name": None, "dob": None, "uid": None, "address": None},
                "confidence": 0.0,
                "warnings": ["OCR dependencies are unavailable in the gateway runtime."],
                "runtime_error": "OCR dependencies are unavailable in the gateway runtime.",
                "text": "",
            },
        )(),
    ):
        evidence = asyncio.run(
            manager.validate_document(
                b"fake-image-bytes",
                "aadhaar",
                verification,
                source,
            )
        )

    assert evidence.provenance.status == "failed"
    assert any(gap.code == "document_processor_failed" for gap in evidence.gaps)


def test_create_sdk_client_returns_distinct_instances_per_call() -> None:
    manager = AgentManager()
    asyncio.run(manager.initialize_agents())
    created_clients = []

    class FakeClient:
        def __init__(self, options):
            self.options = options
            created_clients.append(self)

    with patch("app.agent_manager.ClaudeSDKClient", FakeClient):
        first = manager._create_sdk_client(AgentType.DOCUMENT_VALIDATOR)
        second = manager._create_sdk_client(AgentType.DOCUMENT_VALIDATOR)

    assert first is not second
    assert len(created_clients) == 2


def test_detect_fraud_falls_back_to_deterministic_contract_when_agent_returns_none() -> None:
    manager = AgentManager()
    document = DocumentVerificationEvidence(
        document_type="pan",
        input_kind="raw_document",
        extracted_fields={
            "name": "Alice Example",
            "dob": "1990-01-01",
            "pan_number": "ABCDE1234F",
        },
        submitted_claims={
            "name": "Alice Example",
            "dob": "1990-01-01",
            "pan_number": "ABCDE1234F",
        },
        confidence=0.92,
        warnings=[],
        required_fields=["name", "dob", "pan_number"],
        missing_fields=[],
        provenance=_provenance("document-validator"),
        gaps=[],
    )

    async def fake_invoke_agent(*args, **kwargs):
        del args, kwargs
        return None, _provenance("fraud-detection", status="missing_contract")

    manager.invoke_agent = fake_invoke_agent  # type: ignore[method-assign]

    evidence = asyncio.run(manager.detect_fraud(document, "pan"))

    assert evidence.provenance.status == "completed"
    assert evidence.provenance.model == "deterministic-fallback"
    assert evidence.recommendation == "approve"
    assert evidence.risk_level == "safe"
    assert evidence.gaps == []
    assert any("deterministic fallback" in indicator.lower() for indicator in evidence.indicators)


def test_check_compliance_falls_back_to_deterministic_contract_when_agent_returns_none() -> None:
    manager = AgentManager()
    verification = AadhaarVerificationData(
        name="Alice Example",
        dob="1990-01-01",
        uid="123456789012",
        consent_provided=True,
    )
    document = DocumentVerificationEvidence(
        document_type="aadhaar",
        input_kind="raw_document",
        extracted_fields={
            "name": "Alice Example",
            "dob": "1990-01-01",
            "uid": "123456789012",
        },
        submitted_claims=verification.model_dump(exclude_none=True),
        confidence=0.88,
        warnings=[],
        required_fields=["name", "dob", "uid"],
        missing_fields=[],
        provenance=_provenance("document-validator"),
        gaps=[],
    )

    async def fake_invoke_agent(*args, **kwargs):
        del args, kwargs
        return None, _provenance("compliance-monitor", status="missing_contract")

    manager.invoke_agent = fake_invoke_agent  # type: ignore[method-assign]

    evidence = asyncio.run(manager.check_compliance(document, "aadhaar", verification))

    assert evidence.provenance.status == "completed"
    assert evidence.provenance.model == "deterministic-fallback"
    assert evidence.recommendation == "approve"
    assert evidence.aadhaar_act_compliant is True
    assert evidence.dpdp_compliant is True
    assert evidence.gaps == []
    assert evidence.violations == []


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
