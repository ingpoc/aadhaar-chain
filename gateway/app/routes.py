"""Routes for identity operations with agent integration."""
from fastapi import APIRouter, BackgroundTasks, File, Form, HTTPException, UploadFile
from typing import Optional

from app.models import (
    AuditReceiptReference,
    AttestationArtifact,
    ConsentArtifact,
    IdentityData,
    CreateIdentityRequest,
    CreateIdentityResponse,
    DocumentEvidenceSource,
    ReviewArtifact,
    RevocationArtifact,
    TrustReadSurface,
    TrustVerificationSummary,
    UpdateIdentityRequest,
    VerificationStatus,
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
        raise HTTPException(status_code=404, detail="Identity not found")
    
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


# --- Helper Functions ---


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

    return TrustReadSurface(
        wallet_address=identity.owner,
        did=identity.did,
        verification_bitmap=identity.verification_bitmap,
        updated_at=latest_update,
        verifications=[
            _to_trust_verification_summary(status)
            for status in wallet_verifications
        ],
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
