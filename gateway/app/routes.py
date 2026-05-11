"""Routes for identity operations with agent integration."""
from fastapi import APIRouter, BackgroundTasks, File, Form, HTTPException, Request, UploadFile
from time import monotonic
from typing import Optional

from app.models import (
    AgentRunProvenance,
    AuditReceiptReference,
    AttestationArtifact,
    ComplianceVerificationEvidence,
    ConsentArtifact,
    DocumentVerificationEvidence,
    EvidenceAccessRequest,
    IdentityData,
    CreateIdentityRequest,
    CreateIdentityResponse,
    DocumentEvidenceSource,
    FraudVerificationEvidence,
    ReviewArtifact,
    ReviewDecisionRequest,
    RevocationArtifact,
    RevokeTrustRequest,
    StepStatus,
    TrustReadSurface,
    TrustReadSurfaceResponse,
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
from app.evidence_store import store_encrypted_evidence
from app.state_store import append_audit_event, save_gateway_state
from config import settings


# Runtime stores hydrated on startup.
verifications: dict[str, VerificationStatus] = {}
identities: dict[str, IdentityData] = {}
verification_rate_buckets: dict[str, list[float]] = {}


def persist_runtime_state() -> None:
    """Persist identity and verification state after mutating operations."""
    save_gateway_state(identities, agent_manager.verification_records)


agent_manager.set_state_change_callback(persist_runtime_state)


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
    _enforce_verification_rate_limit(wallet_address)
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
    _store_evidence_if_configured(verification_id, wallet_address, document_data, document_source)
    append_audit_event(
        "verification_requested",
        wallet_address,
        target_id=verification_id,
        target_type="aadhaar_verification",
        details="Aadhaar verification request accepted for agent orchestration.",
    )
    append_audit_event(
        "consent_recorded" if consent_provided else "consent_missing",
        wallet_address,
        target_id=verification_id,
        target_type="consent_record",
        details=(
            "Aadhaar identity-verification consent was explicitly granted."
            if consent_provided
            else "Aadhaar verification was requested without required consent."
        ),
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
    _enforce_verification_rate_limit(wallet_address)
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
    _store_evidence_if_configured(verification_id, wallet_address, document_data, document_source)
    append_audit_event(
        "verification_requested",
        wallet_address,
        target_id=verification_id,
        target_type="pan_verification",
        details="PAN verification request accepted for agent orchestration.",
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


@router.get("/{wallet_address}/trust", response_model=TrustReadSurfaceResponse, tags=["identity"])
async def get_trust_surface(
    wallet_address: str,
):
    """Expose a downstream-safe trust view without leaking raw verification evidence."""
    if wallet_address not in identities:
        append_audit_event(
            "trust_read",
            wallet_address,
            target_id=wallet_address,
            target_type="wallet_trust_surface",
            details="Downstream trust read returned no identity.",
        )
        return TrustReadSurfaceResponse(
            success=True,
            data=_build_no_identity_trust_surface(wallet_address),
        )

    identity = identities[wallet_address]
    trust_surface = _build_trust_surface(identity)
    append_audit_event(
        "trust_read",
        wallet_address,
        target_id=identity.did,
        target_type="wallet_trust_surface",
        details=f"Downstream trust read returned {trust_surface.trust_state}.",
    )
    return TrustReadSurfaceResponse(
        success=True,
        data=trust_surface,
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
    append_audit_event(
        "verification_requested",
        wallet_address,
        target_id=verification_id,
        target_type="aadhaar_verification",
        details="Legacy Aadhaar verification request accepted for agent orchestration.",
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
    append_audit_event(
        "verification_requested",
        wallet_address,
        target_id=verification_id,
        target_type="pan_verification",
        details="Legacy PAN verification request accepted for agent orchestration.",
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
    persist_runtime_state()
    append_audit_event(
        "identity_created",
        wallet_address,
        target_id=identity.did,
        target_type="identity_anchor",
        details="Wallet-bound identity anchor created.",
    )

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
    persist_runtime_state()
    
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
    persist_runtime_state()
    return ApiResponse(
        success=True,
        message=f"Fixture {data.fixture_state} applied",
        data=trust_surface.model_dump() if trust_surface else None,
    )


@router.get("/reviews/queue", response_model=ApiResponse, tags=["identity"])
async def list_review_queue():
    """List downstream-safe manual review queue items for operators."""
    queue_items = []
    for status in agent_manager.verification_records.values():
        if status.status != "manual_review":
            continue
        summary = _to_trust_verification_summary(status)
        queue_items.append(
            {
                "verification_id": status.verification_id,
                "wallet_address": status.wallet_address,
                "workflow_status": status.status,
                "reason": summary.reason,
                "evidence_status": summary.evidence_status,
                "consent": summary.consent.model_dump(),
                "review": summary.review.model_dump(),
                "created_at": status.created_at,
                "updated_at": status.updated_at,
            }
        )

    return ApiResponse(
        success=True,
        message="Manual review queue loaded",
        data={"items": queue_items},
    )


@router.post("/reviews/{verification_id}/evidence-access", response_model=ApiResponse, tags=["identity"])
async def record_evidence_access(
    verification_id: str,
    data: EvidenceAccessRequest,
):
    """Record private-evidence access and return only a redacted descriptor."""
    status = _get_verification_or_404(verification_id)
    metadata = status.metadata
    if metadata is None:
        raise HTTPException(status_code=409, detail="Verification has no evidence metadata")

    audit_event = append_audit_event(
        "evidence_accessed",
        status.wallet_address,
        target_id=verification_id,
        target_type="evidence_reference",
        details=f"Reviewer {data.reviewer_id} accessed redacted evidence for purpose={data.purpose}.",
    )
    return ApiResponse(
        success=True,
        message="Evidence access recorded",
        data={
            "audit_receipt": audit_event["id"],
            "verification_id": verification_id,
            "wallet_address": status.wallet_address,
            "document_type": metadata.document.document_type,
            "evidence_reference": f"evidence:{verification_id}",
            "source": metadata.document.source.model_dump() if metadata.document.source else None,
            "raw_evidence_returned": False,
        },
    )


@router.post("/reviews/{verification_id}/decision", response_model=ApiResponse, tags=["identity"])
async def decide_review(
    verification_id: str,
    data: ReviewDecisionRequest,
):
    """Apply an operator review decision without exposing raw evidence downstream."""
    status = _get_verification_or_404(verification_id)
    if status.status != "manual_review":
        raise HTTPException(status_code=409, detail="Verification is not awaiting manual review")
    if status.metadata is None:
        raise HTTPException(status_code=409, detail="Verification has no decision metadata")

    status.updated_at = _get_timestamp()
    status.metadata.reason = data.reason
    if data.decision == "approve":
        status.status = "verified"
        status.error = None
        status.metadata.decision = "approve"
        identity = identities.get(status.wallet_address)
        if identity:
            identity.verification_bitmap = max(identity.verification_bitmap, 1)
            identity.updated_at = status.updated_at
    elif data.decision == "reject":
        status.status = "failed"
        status.error = data.reason
        status.metadata.decision = "reject"
    else:
        status.status = "manual_review"
        status.error = data.reason
        status.metadata.decision = "manual_review"

    audit_event = append_audit_event(
        "review_decision_recorded",
        status.wallet_address,
        target_id=verification_id,
        target_type="review_record",
        details=(
            f"Reviewer {data.reviewer_id} recorded review decision={data.decision}; "
            f"appeal_reference={data.appeal_reference or 'none'}."
        ),
    )
    persist_runtime_state()
    return ApiResponse(
        success=True,
        message="Review decision recorded",
        data={
            "audit_receipt": audit_event["id"],
            "trust": _build_trust_surface(identities[status.wallet_address]).model_dump()
            if status.wallet_address in identities
            else None,
        },
    )


@router.post("/{wallet_address}/trust/{verification_id}/revoke", response_model=ApiResponse, tags=["identity"])
async def revoke_trust(
    wallet_address: str,
    verification_id: str,
    data: RevokeTrustRequest,
):
    """Revoke a downstream trust artifact and record the operator audit trail."""
    status = _get_verification_or_404(verification_id)
    if status.wallet_address != wallet_address:
        raise HTTPException(status_code=404, detail="Verification not found for wallet")
    if status.status not in {"verified", "manual_review"}:
        raise HTTPException(status_code=409, detail="Only active or reviewable trust can be revoked")

    status.status = "failed"
    status.error = f"Revoked: {data.reason}"
    status.updated_at = _get_timestamp()
    if status.metadata:
        status.metadata.decision = "reject"
        status.metadata.reason = status.error

    audit_event = append_audit_event(
        "trust_revoked",
        wallet_address,
        target_id=verification_id,
        target_type="revocation_record",
        details=f"Operator {data.operator_id} revoked downstream trust: {data.reason}",
    )
    persist_runtime_state()
    return ApiResponse(
        success=True,
        message="Trust revoked",
        data={
            "audit_receipt": audit_event["id"],
            "trust": _build_trust_surface(identities[wallet_address]).model_dump()
            if wallet_address in identities
            else None,
        },
    )


# --- Helper Functions ---


def _ensure_local_fixture_access(request: Request) -> None:
    client_host = request.client.host if request.client else None
    if client_host not in {"127.0.0.1", "::1", "localhost", "testclient", None}:
        raise HTTPException(status_code=403, detail="Trust fixtures are limited to local development access")


def _get_verification_or_404(verification_id: str) -> VerificationStatus:
    status = agent_manager.verification_records.get(verification_id)
    if status is None:
        raise HTTPException(status_code=404, detail="Verification not found")
    return status


def _store_evidence_if_configured(
    verification_id: str,
    wallet_address: str,
    document_data: bytes,
    document_source: DocumentEvidenceSource,
) -> None:
    if not settings.evidence_encryption_key:
        return
    evidence_reference = store_encrypted_evidence(
        verification_id=verification_id,
        wallet_address=wallet_address,
        document_data=document_data,
        source=document_source,
    )
    append_audit_event(
        "evidence_stored",
        wallet_address,
        target_id=verification_id,
        target_type="evidence_reference",
        details=(
            "Uploaded evidence encrypted at rest "
            f"with key_id={evidence_reference['key_id']}."
        ),
    )


def _enforce_verification_rate_limit(wallet_address: str) -> None:
    limit = max(settings.verification_rate_limit_per_minute, 1)
    now = monotonic()
    window_start = now - 60
    bucket = [
        timestamp
        for timestamp in verification_rate_buckets.get(wallet_address, [])
        if timestamp >= window_start
    ]
    if len(bucket) >= limit:
        raise HTTPException(
            status_code=429,
            detail="Verification rate limit exceeded for this wallet.",
        )
    bucket.append(now)
    verification_rate_buckets[wallet_address] = bucket


def _seed_trust_fixture(
    wallet_address: str,
    fixture_state: str,
    document_type: str,
) -> Optional[TrustReadSurface]:
    _clear_wallet_fixture(wallet_address)

    if fixture_state == "no_identity":
        return _build_no_identity_trust_surface(wallet_address)

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


def _build_no_identity_trust_surface(wallet_address: str) -> TrustReadSurface:
    return TrustReadSurface(
        wallet_address=wallet_address,
        did=_build_did(wallet_address),
        verification_bitmap=0,
        updated_at=_get_timestamp(),
        trust_state="no_identity",
        high_trust_eligible=False,
        state_reason="No identity anchor exists for this wallet address.",
        verifications=[],
    )


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
    revocation = _build_revocation_artifact(status, metadata)
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
        if review.reference:
            audit_receipts.append(
                AuditReceiptReference(
                    kind="review_record",
                    reference=review.reference,
                    created_at=status.updated_at,
                )
            )
    if revocation.reference:
        audit_receipts.append(
            AuditReceiptReference(
                kind="revocation_record",
                reference=revocation.reference,
                created_at=status.updated_at,
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
        revocation=revocation,
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


def _build_revocation_artifact(
    status: VerificationStatus,
    metadata: Optional[VerificationMetadata],
) -> RevocationArtifact:
    if status.status == "failed":
        return RevocationArtifact(
            status="active",
            reference=f"revocation:{status.verification_id}",
        )
    if metadata is None:
        return RevocationArtifact(status="pending")
    return RevocationArtifact(status="not_applicable")


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
