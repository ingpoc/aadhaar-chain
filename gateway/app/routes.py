"""Routes for identity operations with agent integration."""
from fastapi import APIRouter, BackgroundTasks, File, Form, HTTPException, Request, UploadFile
from typing import Optional

from app.models import (
    AgentRunProvenance,
    AuditReceiptReference,
    AttestationArtifact,
    ComplianceVerificationEvidence,
    ConsentArtifact,
    DocumentVerificationEvidence,
    IdentityData,
    CreateIdentityRequest,
    CreateIdentityResponse,
    DocumentEvidenceSource,
    FraudVerificationEvidence,
    ReviewArtifact,
    RevocationArtifact,
    StepStatus,
    TrustReadSurface,
    TrustFixtureRequest,
    TrustVerificationSummary,
    UpdateIdentityRequest,
    VerificationMetadata,
    VerificationStatus,
    VerificationStepDetail,
    VerificationStep,
    AadhaarVerificationData,
    PanVerificationData,
    ApiResponse,
)

from app.agent_manager import agent_manager


# In-memory stores (for development)
verifications: dict[str, VerificationStatus] = {}
identities: dict[str, IdentityData] = {}


router = APIRouter(prefix="/api/identity", tags=["identity"])


# --- Verification Routes with Agent Integration ---


@router.post("/{wallet_address}/aadhaar", response_model=ApiResponse, tags=["identity"])
async def create_aadhaar_verification(
    background_tasks: BackgroundTasks,
    wallet_address: str,
    document: UploadFile = File(...),
    name: str = Form(...),
    dob: str = Form(...),
    uid: str = Form(...),
    address: Optional[str] = Form(default=None),
    document_hash: Optional[str] = Form(default=None),
    consent_provided: bool = Form(default=False),
):
    """Create Aadhaar card verification request and start agent workflow."""
    data = AadhaarVerificationData(
        name=name,
        dob=dob,
        uid=uid,
        address=address,
        document_hash=document_hash,
        consent_provided=consent_provided,
    )
    document_data, document_source = await _read_uploaded_document(document, document_hash)

    verification_id = await agent_manager.create_verification(
        wallet_address,
        "aadhaar",
        data
    )
    background_tasks.add_task(
        agent_manager.orchestrate_verification,
        wallet_address,
        "aadhaar",
        document_data,
        data,
        document_source,
    )
    
    return ApiResponse(
        success=True,
        message=f"Aadhaar verification created: {verification_id}",
        data={
            "verification_id": verification_id,
            "status": "document_received"
        }
    )


@router.post("/{wallet_address}/pan", response_model=ApiResponse, tags=["identity"])
async def create_pan_verification(
    background_tasks: BackgroundTasks,
    wallet_address: str,
    document: UploadFile = File(...),
    name: str = Form(...),
    pan_number: str = Form(...),
    dob: str = Form(...),
    document_hash: Optional[str] = Form(default=None),
):
    """Create PAN card verification request and start agent workflow."""
    data = PanVerificationData(
        name=name,
        pan_number=pan_number,
        dob=dob,
        document_hash=document_hash,
    )
    document_data, document_source = await _read_uploaded_document(document, document_hash)

    verification_id = await agent_manager.create_verification(
        wallet_address,
        "pan",
        data
    )
    background_tasks.add_task(
        agent_manager.orchestrate_verification,
        wallet_address,
        "pan",
        document_data,
        data,
        document_source,
    )
    
    return ApiResponse(
        success=True,
        message=f"PAN verification created: {verification_id}",
        data={
            "verification_id": verification_id,
            "status": "document_received"
        }
    )


@router.get("/status/{verification_id}", response_model=ApiResponse, tags=["identity"])
async def get_verification_status(
    verification_id: str,
):
    """Get verification status by ID."""
    status = await agent_manager.get_verification_status(verification_id)
    
    if not status:
        raise HTTPException(status_code=404, detail="Verification not found")
    
    return ApiResponse(
        success=True,
        data=status.model_dump()
    )


@router.get("/{wallet_address}/trust", response_model=ApiResponse, tags=["identity"])
async def get_trust_surface(
    wallet_address: str,
):
    """Expose a downstream-safe trust view without leaking raw verification evidence."""
    if wallet_address not in identities:
        raise HTTPException(status_code=404, detail="Identity not found")

    identity = identities[wallet_address]
    trust_surface = _build_trust_surface(identity)
    return ApiResponse(
        success=True,
        data=trust_surface.model_dump(),
    )


# --- Document Verification Routes with Agent Orchestration ---


@router.post("/verify/aadhaar", response_model=ApiResponse, tags=["identity"])
async def verify_aadhaar_document(
    wallet_address: str,
    document_data: bytes,  # Base64 encoded document data
    verification_data: Optional[dict] = None,
):
    """Verify Aadhaar card document using agent workflow."""
    
    # Create verification request
    verification_id = await agent_manager.create_verification(
        wallet_address,
        "aadhaar",
        verification_data
    )
    
    # Orchestrate verification workflow through agents
    status = await agent_manager.orchestrate_verification(
        wallet_address,
        "aadhaar",
        document_data,
        verification_data
    )
    
    return ApiResponse(
        success=True,
        message=f"Aadhaar verification {status.current_step.value}",
        data={
            "verification_id": verification_id,
            "status": status.current_step.value,
            "progress": status.progress,
            "decision": status.metadata.decision if status.metadata else None,
        }
    )


@router.post("/verify/pan", response_model=ApiResponse, tags=["identity"])
async def verify_pan_document(
    wallet_address: str,
    document_data: bytes,  # Base64 encoded document data
    verification_data: Optional[dict] = None,
):
    """Verify PAN card document using agent workflow."""
    
    # Create verification request
    verification_id = await agent_manager.create_verification(
        wallet_address,
        "pan",
        verification_data
    )
    
    # Orchestrate verification workflow through agents
    status = await agent_manager.orchestrate_verification(
        wallet_address,
        "pan",
        document_data,
        verification_data
    )
    
    return ApiResponse(
        success=True,
        message=f"PAN verification {status.current_step.value}",
        data={
            "verification_id": verification_id,
            "status": status.current_step.value,
            "progress": status.progress,
            "decision": status.metadata.decision if status.metadata else None,
        }
    )


# --- Identity Routes ---


@router.get("/{wallet_address}", response_model=ApiResponse, tags=["identity"])
async def get_identity(
    wallet_address: str,
):
    """Get identity data for wallet address."""
    if wallet_address not in identities:
        return ApiResponse(
            success=True,
            message="Identity not found",
            data=None,
        )

    return ApiResponse(
        success=True,
        data=identities[wallet_address].model_dump()
    )


@router.post("/{wallet_address}", response_model=ApiResponse, tags=["identity"])
async def create_identity(
    wallet_address: str,
    data: CreateIdentityRequest,
):
    """Create a new identity anchor for the wallet address."""
    if wallet_address in identities:
        raise HTTPException(status_code=409, detail="Identity already exists")

    identity = IdentityData(
        did=_build_did(wallet_address),
        owner=wallet_address,
        commitment=data.commitment,
        verification_bitmap=0,
        created_at=_get_timestamp(),
        updated_at=_get_timestamp(),
    )
    identities[wallet_address] = identity

    return ApiResponse(
        success=True,
        message="Identity created",
        data=CreateIdentityResponse(identity=identity).model_dump()
    )


@router.patch("/{wallet_address}", response_model=ApiResponse, tags=["identity"])
async def update_identity(
    wallet_address: str,
    data: UpdateIdentityRequest,
):
    """Update identity data for wallet address."""
    if wallet_address not in identities:
        raise HTTPException(status_code=404, detail="Identity not found")
    
    identity = identities[wallet_address]

    if data.commitment is not None:
        identity.commitment = data.commitment

    if data.verification_bitmap is not None:
        identity.verification_bitmap = data.verification_bitmap
    
    identity.updated_at = _get_timestamp()
    
    return ApiResponse(
        success=True,
        message="Identity updated",
        data=identity.model_dump()
    )


@router.post("/dev/fixtures/{wallet_address}", response_model=ApiResponse, tags=["identity"])
async def seed_trust_fixture(
    wallet_address: str,
    request: Request,
    data: TrustFixtureRequest,
):
    """Seed deterministic local trust states for downstream browser validation."""
    _ensure_local_fixture_access(request)
    trust_surface = _seed_trust_fixture(wallet_address, data.fixture_state, data.document_type)
    return ApiResponse(
        success=True,
        message=f"Fixture {data.fixture_state} applied",
        data=trust_surface.model_dump() if trust_surface else None,
    )


# --- Helper Functions ---


def _ensure_local_fixture_access(request: Request) -> None:
    client_host = request.client.host if request.client else None
    if client_host not in {"127.0.0.1", "::1", "localhost", "testclient", None}:
        raise HTTPException(status_code=403, detail="Trust fixtures are limited to local development access")


def _seed_trust_fixture(
    wallet_address: str,
    fixture_state: str,
    document_type: str,
) -> Optional[TrustReadSurface]:
    _clear_wallet_fixture(wallet_address)

    if fixture_state == "no_identity":
        return None

    identities[wallet_address] = IdentityData(
        did=_build_did(wallet_address),
        owner=wallet_address,
        commitment=f"sha256:{fixture_state}:{wallet_address}",
        verification_bitmap=1 if fixture_state == "verified" else 0,
        created_at=_get_timestamp(),
        updated_at=_get_timestamp(),
    )

    if fixture_state == "identity_present_unverified":
        return _build_trust_surface(identities[wallet_address])

    verification_id = f"{document_type}_{wallet_address}_fixture"
    agent_manager.verification_records[verification_id] = VerificationStatus(
        verification_id=verification_id,
        wallet_address=wallet_address,
        status=_fixture_workflow_status(fixture_state),  # type: ignore[arg-type]
        current_step=VerificationStep.complete,
        steps=[
            VerificationStepDetail(name="document_received", status=StepStatus.completed),
            VerificationStepDetail(name="parsing", status=StepStatus.completed),
            VerificationStepDetail(name="fraud_check", status=StepStatus.completed),
            VerificationStepDetail(name="compliance_check", status=StepStatus.completed),
            VerificationStepDetail(name="blockchain_upload", status=StepStatus.completed),
        ],
        progress=1.0,
        created_at=_get_timestamp(),
        updated_at=_get_timestamp(),
        error=_fixture_reason(fixture_state) if fixture_state == "revoked_or_blocked" else None,
        metadata=_build_fixture_metadata(fixture_state, document_type),
    )
    return _build_trust_surface(identities[wallet_address])


def _clear_wallet_fixture(wallet_address: str) -> None:
    identities.pop(wallet_address, None)
    for verification_id, status in list(agent_manager.verification_records.items()):
        if status.wallet_address == wallet_address:
            del agent_manager.verification_records[verification_id]


def _fixture_workflow_status(fixture_state: str) -> str:
    if fixture_state == "verified":
        return "verified"
    if fixture_state == "manual_review":
        return "manual_review"
    return "failed"


def _fixture_reason(fixture_state: str) -> str:
    return {
        "verified": "Verification approved for local trust matrix testing.",
        "manual_review": "Verification escalated for manual review in local trust matrix testing.",
        "revoked_or_blocked": "Verification was blocked for local trust matrix testing.",
    }[fixture_state]


def _fixture_provenance(agent_id: str) -> AgentRunProvenance:
    timestamp = _get_timestamp()
    return AgentRunProvenance(
        agent_id=agent_id,
        status="completed",
        started_at=timestamp,
        completed_at=timestamp,
        tools=[],
    )


def _build_fixture_metadata(fixture_state: str, document_type: str) -> VerificationMetadata:
    decision = {
        "verified": "approve",
        "manual_review": "manual_review",
        "revoked_or_blocked": "reject",
    }[fixture_state]
    consent_provided = document_type == "pan" or fixture_state != "revoked_or_blocked"

    return VerificationMetadata(
        decision=decision,  # type: ignore[arg-type]
        reason=_fixture_reason(fixture_state),
        evidence_status="complete",
        document=DocumentVerificationEvidence(
            document_type=document_type,  # type: ignore[arg-type]
            input_kind="request_payload",
            source=None,
            extracted_fields={"document_type": document_type},
            submitted_claims={"consent_provided": consent_provided},
            confidence=0.98,
            warnings=[],
            required_fields=["name"],
            missing_fields=[],
            provenance=_fixture_provenance("document-validator"),
            gaps=[],
        ),
        fraud=FraudVerificationEvidence(
            risk_score=0.08 if fixture_state == "verified" else 0.62 if fixture_state == "manual_review" else 0.94,
            risk_level="low" if fixture_state == "verified" else "medium" if fixture_state == "manual_review" else "high",
            indicators=[] if fixture_state == "verified" else ["identity_mismatch"] if fixture_state == "manual_review" else ["revocation_marker"],
            recommendation="approve" if fixture_state == "verified" else "manual_review" if fixture_state == "manual_review" else "block",
            provenance=_fixture_provenance("fraud-detection"),
            gaps=[],
        ),
        compliance=ComplianceVerificationEvidence(
            aadhaar_act_compliant=fixture_state != "revoked_or_blocked",
            dpdp_compliant=fixture_state != "revoked_or_blocked",
            violations=[] if fixture_state != "revoked_or_blocked" else ["local_fixture_revocation"],
            recommendation="approve" if fixture_state == "verified" else "manual_review" if fixture_state == "manual_review" else "block",
            provenance=_fixture_provenance("compliance-monitor"),
            gaps=[],
        ),
        blocking_gaps=[],
        assumptions=["Local trust fixture for deterministic browser validation."],
    )


def _get_timestamp() -> str:
    """Get current timestamp in ISO format."""
    from datetime import datetime
    return datetime.utcnow().isoformat() + "Z"


def _build_did(wallet_address: str) -> str:
    """Build a stable DID-like identifier from the wallet address."""
    return f"did:solana:{wallet_address}"


def _build_trust_surface(identity: IdentityData) -> TrustReadSurface:
    wallet_verifications = [
        status
        for status in agent_manager.verification_records.values()
        if status.wallet_address == identity.owner
    ]
    wallet_verifications.sort(key=lambda status: status.updated_at, reverse=True)
    latest_update = wallet_verifications[0].updated_at if wallet_verifications else identity.updated_at
    verification_summaries = [
        _to_trust_verification_summary(status)
        for status in wallet_verifications
    ]
    trust_state, state_reason = _derive_portfolio_trust_state(verification_summaries)

    return TrustReadSurface(
        wallet_address=identity.owner,
        did=identity.did,
        verification_bitmap=identity.verification_bitmap,
        updated_at=latest_update,
        trust_state=trust_state,
        high_trust_eligible=trust_state == "verified",
        state_reason=state_reason,
        verifications=verification_summaries,
    )


def _to_trust_verification_summary(status: VerificationStatus) -> TrustVerificationSummary:
    document_type = "aadhaar" if status.verification_id.startswith("aadhaar_") else "pan"
    metadata = status.metadata
    consent = _build_consent_artifact(status, document_type)
    review = _build_review_artifact(status)
    audit_receipts = [
        AuditReceiptReference(
            kind="verification_record",
            reference=f"verification:{status.verification_id}",
            created_at=status.created_at,
        )
    ]
    if metadata is not None:
        audit_receipts.append(
            AuditReceiptReference(
                kind="decision_record",
                reference=f"decision:{status.verification_id}",
                created_at=status.updated_at,
            )
        )
        if consent.reference:
            audit_receipts.append(
                AuditReceiptReference(
                    kind="consent_record",
                    reference=consent.reference,
                    created_at=status.created_at,
                )
            )

    return TrustVerificationSummary(
        document_type=document_type,  # type: ignore[arg-type]
        verification_id=status.verification_id,
        workflow_status=status.status,
        decision=metadata.decision if metadata else None,
        reason=metadata.reason if metadata else status.error,
        evidence_status=metadata.evidence_status if metadata else None,
        consent=consent,
        attestation=AttestationArtifact(
            status="not_issued" if metadata else "pending",
            credential_type=document_type,
            reference=None,
        ),
        revocation=RevocationArtifact(
            status="not_applicable" if metadata else "pending",
            reference=None,
        ),
        review=review,
        audit_receipts=audit_receipts,
    )


def _build_consent_artifact(
    status: VerificationStatus,
    document_type: str,
) -> ConsentArtifact:
    if document_type != "aadhaar":
        return ConsentArtifact(status="not_required")

    if status.metadata is None:
        return ConsentArtifact(
            status="pending",
            scope="aadhaar_identity_verification",
            purpose="identity_verification",
        )

    consent_provided = bool(
        status.metadata.document.submitted_claims.get("consent_provided", False)
    )
    return ConsentArtifact(
        status="granted" if consent_provided else "missing",
        scope="aadhaar_identity_verification",
        purpose="identity_verification",
        reference=f"consent:{status.verification_id}" if consent_provided else None,
    )


def _build_review_artifact(status: VerificationStatus) -> ReviewArtifact:
    if status.status == "manual_review":
        return ReviewArtifact(
            status="manual_review_required",
            reference=f"review:{status.verification_id}",
            reason=status.error,
        )
    if status.status == "verified":
        return ReviewArtifact(
            status="approved",
            reference=f"review:{status.verification_id}",
            reason=status.metadata.reason if status.metadata else None,
        )
    if status.status == "failed":
        return ReviewArtifact(
            status="rejected",
            reference=f"review:{status.verification_id}",
            reason=status.error,
        )
    return ReviewArtifact(status="pending")


def _derive_portfolio_trust_state(
    verifications: list[TrustVerificationSummary],
) -> tuple[str, Optional[str]]:
    if not verifications:
        return "identity_present_unverified", "Identity anchor exists, but no approved verification is available yet."

    for verification in verifications:
        if verification.revocation.status in {"active", "revoked"}:
            return "revoked_or_blocked", verification.reason or "Trust state is no longer active."
        if verification.review.status == "rejected":
            return "revoked_or_blocked", verification.reason or "Verification was rejected."

    for verification in verifications:
        if verification.workflow_status == "verified" and verification.review.status == "approved":
            return "verified", verification.reason or "Verification approved and available for downstream use."

    for verification in verifications:
        if (
            verification.workflow_status == "manual_review"
            or verification.review.status == "manual_review_required"
        ):
            return "manual_review", verification.reason or "Verification requires manual review."

    return "identity_present_unverified", verifications[0].reason or "Identity exists, but trust is not yet elevated."


async def _read_uploaded_document(
    document: UploadFile,
    submitted_hash: Optional[str],
) -> tuple[bytes, DocumentEvidenceSource]:
    """Read uploaded document bytes and build a stable source descriptor."""
    document_data = await document.read()
    if not document_data:
        raise HTTPException(status_code=400, detail="Document file is required")

    document_source = agent_manager.build_document_source(
        "upload",
        document_data,
        file_name=document.filename,
        content_type=document.content_type,
        submitted_hash=submitted_hash,
    )
    return document_data, document_source
