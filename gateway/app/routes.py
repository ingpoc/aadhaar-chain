"""Routes for identity operations with agent integration."""
from fastapi import APIRouter, BackgroundTasks, File, Form, HTTPException, Request, Response, UploadFile
from pydantic import BaseModel, Field
import json
import secrets
from time import monotonic
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from app.models import (
    AgentRunProvenance,
    AuditReceiptReference,
    AttestationArtifact,
    ComplianceVerificationEvidence,
    ConsentArtifact,
    DocumentVerificationEvidence,
    EvidenceAccessRequest,
    IdentityData,
    IdentityProofToken,
    IdentityProofTokenRequest,
    CreateIdentityRequest,
    CreateIdentityResponse,
    DocumentEvidenceSource,
    FraudVerificationEvidence,
    ReviewArtifact,
    ReviewDecisionRequest,
    RevocationArtifact,
    RevokeTrustRequest,
    SignedIdentityProofRequest,
    SignedIdentityProofResult,
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
from app.session_auth import create_session_token, set_session_cookie
from app.setu_ekyc import (
    create_ekyc_request,
    get_ekyc_request,
    load_ekyc_link,
    save_ekyc_link,
    setu_ekyc_configured,
)
from app.state_store import append_audit_event, save_gateway_state
from config import settings


class SetuEkycStartRequest(BaseModel):
    consent_provided: bool = False


class SetuEkycSyncRequest(BaseModel):
    setu_id: str = Field(min_length=8)


# Runtime stores hydrated on startup.
verifications: dict[str, VerificationStatus] = {}
identities: dict[str, IdentityData] = {}
verification_rate_buckets: dict[str, list[float]] = {}
identity_proof_tokens: dict[str, IdentityProofToken] = {}


def persist_runtime_state() -> None:
    """Persist identity and verification state after mutating operations."""
    save_gateway_state(identities, agent_manager.verification_records)


agent_manager.set_state_change_callback(persist_runtime_state)


async def _apply_on_chain_verification_bitmap(
    wallet_address: str,
    document_type: str,
    chain_signature: Optional[str],
) -> None:
    """Sync local trust bitmap after a confirmed on-chain verification update."""
    from app.solana_bridge import verification_bit

    identity = identities.get(wallet_address)
    if identity is None:
        return

    identity.verification_bitmap |= verification_bit(document_type)
    identity.updated_at = _get_timestamp()
    persist_runtime_state()
    if chain_signature:
        append_audit_event(
            "blockchain_upload",
            wallet_address,
            target_id=identity.did,
            target_type="identity_anchor",
            details=f"On-chain verification bitmap updated ({document_type}): {chain_signature}",
        )


def _register_solana_bridge_hooks() -> None:
    if not settings.solana_on_chain_enabled:
        return
    from app.solana_bridge import get_solana_bridge

    get_solana_bridge().set_on_chain_approved_handler(_apply_on_chain_verification_bitmap)


_register_solana_bridge_hooks()


router = APIRouter(prefix="/api/identity", tags=["identity"])


# --- Setu.co Aadhaar eKYC (env-gated) ---


@router.get("/ekyc/config", response_model=ApiResponse, tags=["identity"])
async def get_ekyc_config():
    """Frontend feature flag: live Setu eKYC vs demo upload path."""
    return ApiResponse(
        success=True,
        data={
            "provider": "setu_ekyc",
            "enabled": setu_ekyc_configured(),
            "base_url": settings.setu_ekyc_base_url if setu_ekyc_configured() else None,
        },
    )


@router.post("/{wallet_address}/aadhaar/ekyc/start", response_model=ApiResponse, tags=["identity"])
async def start_setu_aadhaar_ekyc(
    wallet_address: str,
    body: SetuEkycStartRequest,
):
    """Create Setu eKYC request and return hosted kycURL for OTP flow."""
    if not setu_ekyc_configured():
        raise HTTPException(
            status_code=503,
            detail="Setu eKYC is not configured. Set SETU_EKYC_* in gateway .env.",
        )
    if wallet_address not in identities:
        raise HTTPException(status_code=404, detail="Identity anchor not found")
    if not body.consent_provided:
        raise HTTPException(status_code=400, detail="Consent is required for Aadhaar eKYC.")

    _enforce_verification_rate_limit(wallet_address)

    verification_id = await agent_manager.create_verification(wallet_address, "aadhaar", None)
    await agent_manager.update_verification_progress(
        verification_id,
        VerificationStep.document_received,
        0.15,
    )

    webhook_url = f"{settings.public_gateway_url.rstrip('/')}/api/identity/webhooks/setu/ekyc"
    redirection_url = (
        f"{settings.public_web_url.rstrip('/')}/verify"
        f"?ekyc=setu&wallet={wallet_address}"
    )

    try:
        setu_payload = create_ekyc_request(
            webhook_url=webhook_url,
            redirection_url=redirection_url,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    setu_id = str(setu_payload.get("id") or "")
    kyc_url = str(setu_payload.get("kycURL") or setu_payload.get("kycUrl") or "")
    if not setu_id or not kyc_url:
        raise HTTPException(status_code=502, detail="Setu eKYC response missing id/kycURL")

    save_ekyc_link(
        setu_id=setu_id,
        wallet_address=wallet_address,
        verification_id=verification_id,
    )
    append_audit_event(
        "verification_requested",
        wallet_address,
        target_id=verification_id,
        target_type="aadhaar_ekyc",
        details=f"Setu eKYC started setu_id={setu_id}.",
    )
    append_audit_event(
        "consent_recorded",
        wallet_address,
        target_id=verification_id,
        target_type="consent_record",
        details="Aadhaar eKYC consent granted before Setu redirect.",
    )

    return ApiResponse(
        success=True,
        message="Setu eKYC request created",
        data={
            "verification_id": verification_id,
            "setu_id": setu_id,
            "kyc_url": kyc_url,
            "status": setu_payload.get("status") or "CREATED",
        },
    )


@router.post("/webhooks/setu/ekyc", response_model=ApiResponse, tags=["identity"])
async def setu_ekyc_webhook(request: Request):
    """Receive Setu EKYC_DATA webhook and complete local verification."""
    try:
        payload = await request.json()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail="Invalid JSON body") from exc

    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        data = payload if isinstance(payload, dict) else {}
    setu_id = str(data.get("id") or "")
    if not setu_id:
        raise HTTPException(status_code=400, detail="Missing Setu request id")

    result = await _finalize_setu_ekyc(setu_id, data)
    return ApiResponse(success=True, message="Webhook processed", data=result)


@router.post("/ekyc/sync", response_model=ApiResponse, tags=["identity"])
async def sync_setu_ekyc(body: SetuEkycSyncRequest):
    """Poll Setu and finalize after browser redirect (webhook may lag)."""
    if not setu_ekyc_configured():
        raise HTTPException(status_code=503, detail="Setu eKYC is not configured.")
    try:
        remote = get_ekyc_request(body.setu_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    result = await _finalize_setu_ekyc(body.setu_id, remote)
    return ApiResponse(success=True, message="Sync processed", data=result)


async def _finalize_setu_ekyc(setu_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    link = load_ekyc_link(setu_id)
    if link is None:
        raise HTTPException(status_code=404, detail="Unknown Setu eKYC request id")

    verification_id = link["verification_id"]
    wallet_address = link["wallet_address"]
    status = str(payload.get("status") or "").upper()

    existing = await agent_manager.get_verification_status(verification_id)
    if existing and existing.status in {"verified", "failed", "manual_review"}:
        return {
            "verification_id": verification_id,
            "wallet_address": wallet_address,
            "setu_id": setu_id,
            "status": existing.status,
            "setu_status": status,
        }

    if status in {"CREATED", "KYC_REQUESTED", "PENDING", ""}:
        await agent_manager.update_verification_progress(
            verification_id,
            VerificationStep.parsing,
            0.4,
        )
        return {
            "verification_id": verification_id,
            "wallet_address": wallet_address,
            "setu_id": setu_id,
            "status": "processing",
            "setu_status": status or "PENDING",
        }

    if status == "SUCCESS":
        metadata = _build_setu_success_metadata(setu_id, payload)
        await agent_manager.complete_verification(verification_id, "approve", metadata)
        append_audit_event(
            "verification_decision",
            wallet_address,
            target_id=verification_id,
            target_type="aadhaar_ekyc",
            details=f"Setu eKYC SUCCESS setu_id={setu_id}.",
        )
        return {
            "verification_id": verification_id,
            "wallet_address": wallet_address,
            "setu_id": setu_id,
            "status": "verified",
            "setu_status": status,
        }

    metadata = _build_setu_failure_metadata(setu_id, payload, status)
    await agent_manager.complete_verification(verification_id, "reject", metadata)
    return {
        "verification_id": verification_id,
        "wallet_address": wallet_address,
        "setu_id": setu_id,
        "status": "failed",
        "setu_status": status,
    }


def _format_setu_address(address: Any) -> Optional[str]:
    if not isinstance(address, dict):
        return None
    parts = [
        address.get("house"),
        address.get("street"),
        address.get("locality"),
        address.get("landmark"),
        address.get("vtc"),
        address.get("district"),
        address.get("state"),
        address.get("pin"),
        address.get("country"),
    ]
    joined = ", ".join(str(part).strip() for part in parts if part)
    return joined or None


def _build_setu_success_metadata(setu_id: str, payload: dict[str, Any]) -> VerificationMetadata:
    aadhaar = payload.get("aadhaar") if isinstance(payload.get("aadhaar"), dict) else {}
    extracted = {
        "name": aadhaar.get("name"),
        "dob": aadhaar.get("dateOfBirth"),
        "gender": aadhaar.get("gender"),
        "uid_masked": aadhaar.get("aadhaarNumber") or aadhaar.get("maskedAadhaarNumber"),
        "address": _format_setu_address(aadhaar.get("address")),
        "setu_id": setu_id,
        "provider": "setu_ekyc",
    }
    return VerificationMetadata(
        decision="approve",
        reason="Setu Aadhaar eKYC completed successfully.",
        evidence_status="complete",
        document=DocumentVerificationEvidence(
            document_type="aadhaar",
            input_kind="request_payload",
            source=DocumentEvidenceSource(transport="request_payload"),
            extracted_fields={k: v for k, v in extracted.items() if v is not None},
            submitted_claims={"consent_provided": True, "provider": "setu_ekyc"},
            confidence=0.99,
            warnings=[],
            required_fields=["name"],
            missing_fields=[],
            provenance=_fixture_provenance("setu-ekyc"),
            gaps=[],
        ),
        fraud=FraudVerificationEvidence(
            risk_score=0.05,
            risk_level="low",
            indicators=[],
            recommendation="approve",
            provenance=_fixture_provenance("setu-ekyc"),
            gaps=[],
        ),
        compliance=ComplianceVerificationEvidence(
            aadhaar_act_compliant=True,
            dpdp_compliant=True,
            violations=[],
            recommendation="approve",
            provenance=_fixture_provenance("setu-ekyc"),
            gaps=[],
        ),
        blocking_gaps=[],
        assumptions=["Aadhaar OTP verification performed by Setu.co; gateway stores masked fields only."],
    )


def _build_setu_failure_metadata(
    setu_id: str,
    payload: dict[str, Any],
    status: str,
) -> VerificationMetadata:
    reason = str(payload.get("error") or payload.get("message") or f"Setu eKYC ended with status={status}")
    return VerificationMetadata(
        decision="reject",
        reason=reason,
        evidence_status="partial",
        document=DocumentVerificationEvidence(
            document_type="aadhaar",
            input_kind="request_payload",
            source=DocumentEvidenceSource(transport="request_payload"),
            extracted_fields={"setu_id": setu_id, "provider": "setu_ekyc", "setu_status": status},
            submitted_claims={"consent_provided": True, "provider": "setu_ekyc"},
            confidence=None,
            warnings=[reason],
            required_fields=["name"],
            missing_fields=["name"],
            provenance=_fixture_provenance("setu-ekyc"),
            gaps=[],
        ),
        fraud=FraudVerificationEvidence(
            risk_score=None,
            risk_level=None,
            indicators=[],
            recommendation="block",
            provenance=_fixture_provenance("setu-ekyc"),
            gaps=[],
        ),
        compliance=ComplianceVerificationEvidence(
            aadhaar_act_compliant=None,
            dpdp_compliant=None,
            violations=[],
            recommendation="block",
            provenance=_fixture_provenance("setu-ekyc"),
            gaps=[],
        ),
        blocking_gaps=[],
        assumptions=[],
    )


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


@router.post("/{wallet_address}/proof-token", response_model=ApiResponse, tags=["identity"])
async def issue_identity_proof_token(
    wallet_address: str,
    data: IdentityProofTokenRequest,
):
    """Issue a signed-message challenge for downstream buyer/seller proof or SSO login."""
    is_sso_login = data.purpose == "sso_login"

    if is_sso_login:
        if wallet_address in identities:
            trust_surface = _build_trust_surface(identities[wallet_address])
        else:
            trust_surface = _build_no_identity_trust_surface(wallet_address)
    else:
        if wallet_address not in identities:
            raise HTTPException(status_code=404, detail="Identity anchor not found")

        trust_surface = _build_trust_surface(identities[wallet_address])
        if trust_surface.trust_state != "verified":
            raise HTTPException(
                status_code=403,
                detail="Verified AadhaarChain trust is required before issuing an identity proof token.",
            )

    token = _build_identity_proof_token(wallet_address, data, trust_surface)
    identity_proof_tokens[token.token_id] = token
    append_audit_event(
        "identity_proof_token_issued",
        wallet_address,
        target_id=token.token_id,
        target_type="identity_proof_token",
        details=f"Issued {data.audience} identity proof token ({data.purpose}).",
    )
    return ApiResponse(
        success=True,
        message="Identity proof token issued",
        data=token.model_dump(),
    )


@router.post("/proof-token/verify", response_model=ApiResponse, tags=["identity"])
async def verify_identity_proof_token(
    data: SignedIdentityProofRequest,
    response: Response,
):
    """Verify a wallet signature over an AadhaarChain proof token."""
    result = _verify_signed_identity_proof(data)
    token = identity_proof_tokens.get(data.token_id)
    append_audit_event(
        "identity_proof_token_verified" if result.valid else "identity_proof_token_rejected",
        data.wallet_address,
        target_id=data.token_id,
        target_type="identity_proof_token",
        details=result.reason,
    )

    if result.valid and token is not None and token.purpose == "sso_login":
        did = _build_did(data.wallet_address)
        if data.wallet_address in identities:
            did = identities[data.wallet_address].did
        else:
            if settings.aadhaar_chain_env == "demo":
                _seed_trust_fixture(data.wallet_address, "verified", "aadhaar")
                did = identities[data.wallet_address].did
        session_token = create_session_token(
            wallet_address=data.wallet_address,
            did=did,
            audience=token.audience,
        )
        set_session_cookie(response, session_token)
        append_audit_event(
            "identity_session_created",
            data.wallet_address,
            target_id=data.token_id,
            target_type="identity_session",
            details=f"Portfolio SSO session issued for audience={token.audience}.",
        )

    return ApiResponse(
        success=result.valid,
        message=result.reason,
        data=result.model_dump(),
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

    unsigned_transaction: Optional[str] = None
    if settings.solana_on_chain_enabled:
        from app.solana_bridge import get_solana_bridge

        bridge = get_solana_bridge()
        metadata_uri = f"aadhaarchain://commitment/{data.commitment}"
        try:
            unsigned_transaction = await bridge.build_create_identity_transaction(
                wallet_address,
                identity.did,
                metadata_uri,
            )
        except Exception as exc:  # noqa: BLE001
            identities.pop(wallet_address, None)
            persist_runtime_state()
            raise HTTPException(
                status_code=503,
                detail=f"Failed to prepare on-chain identity transaction: {exc}",
            ) from exc

    return ApiResponse(
        success=True,
        message="Identity created",
        data=CreateIdentityResponse(
            identity=identity,
            signature=unsigned_transaction,
        ).model_dump(),
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
        document_type = "aadhaar" if verification_id.startswith("aadhaar_") else "pan"
        if status.metadata is not None:
            approving_metadata = status.metadata.model_copy(
                update={"decision": "approve", "reason": data.reason},
            )
            status.metadata = await agent_manager._submit_on_chain_verification(
                status.wallet_address,
                document_type,
                approving_metadata,
            )
        if status.metadata and status.metadata.decision == "approve":
            status.status = "verified"
            status.error = None
            status.metadata.decision = "approve"
        else:
            status.status = "manual_review"
            status.error = status.metadata.reason if status.metadata else data.reason
            if status.metadata:
                status.metadata.decision = "manual_review"
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


def _build_identity_proof_token(
    wallet_address: str,
    request: IdentityProofTokenRequest,
    trust_surface: TrustReadSurface,
) -> IdentityProofToken:
    issued_at = datetime.now(timezone.utc)
    expires_at = issued_at + timedelta(minutes=5)
    token_id = f"idproof_{secrets.token_urlsafe(18)}"
    message_payload = {
        "audience": request.audience,
        "did": trust_surface.did,
        "expires_at": expires_at.isoformat().replace("+00:00", "Z"),
        "high_trust_eligible": trust_surface.high_trust_eligible,
        "issued_at": issued_at.isoformat().replace("+00:00", "Z"),
        "purpose": request.purpose,
        "token_id": token_id,
        "trust_state": trust_surface.trust_state,
        "trust_version": trust_surface.trust_version,
        "wallet_address": wallet_address,
    }

    return IdentityProofToken(
        token_id=token_id,
        wallet_address=wallet_address,
        audience=request.audience,
        purpose=request.purpose,
        trust_state=trust_surface.trust_state,
        high_trust_eligible=trust_surface.high_trust_eligible,
        issued_at=message_payload["issued_at"],
        expires_at=message_payload["expires_at"],
        message=json.dumps(message_payload, separators=(",", ":"), sort_keys=True),
    )


def _verify_signed_identity_proof(data: SignedIdentityProofRequest) -> SignedIdentityProofResult:
    verified_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    token = identity_proof_tokens.get(data.token_id)
    if token is None:
        return SignedIdentityProofResult(
            valid=False,
            wallet_address=data.wallet_address,
            audience=data.audience,
            reason="Identity proof token was not issued by this AadhaarChain gateway.",
            verified_at=verified_at,
        )

    if token.wallet_address != data.wallet_address or token.audience != data.audience:
        return SignedIdentityProofResult(
            valid=False,
            wallet_address=data.wallet_address,
            audience=data.audience,
            reason="Identity proof token subject or audience does not match the signed request.",
            verified_at=verified_at,
        )

    if token.message != data.message:
        return SignedIdentityProofResult(
            valid=False,
            wallet_address=data.wallet_address,
            audience=data.audience,
            reason="Signed message does not match the issued identity proof token.",
            verified_at=verified_at,
        )

    expires_at = datetime.fromisoformat(token.expires_at.replace("Z", "+00:00"))
    if expires_at < datetime.now(timezone.utc):
        return SignedIdentityProofResult(
            valid=False,
            wallet_address=data.wallet_address,
            audience=data.audience,
            trust_state=token.trust_state,
            high_trust_eligible=token.high_trust_eligible,
            reason="Identity proof token has expired.",
            verified_at=verified_at,
        )

    try:
        from solders.pubkey import Pubkey
        from solders.signature import Signature

        public_key = Pubkey.from_string(data.wallet_address)
        signature = Signature.from_string(data.signature)
    except Exception:
        return SignedIdentityProofResult(
            valid=False,
            wallet_address=data.wallet_address,
            audience=data.audience,
            trust_state=token.trust_state,
            high_trust_eligible=token.high_trust_eligible,
            reason="Identity proof token contains an invalid wallet address or signature.",
            verified_at=verified_at,
        )

    if not signature.verify(public_key, data.message.encode("utf-8")):
        return SignedIdentityProofResult(
            valid=False,
            wallet_address=data.wallet_address,
            audience=data.audience,
            trust_state=token.trust_state,
            high_trust_eligible=token.high_trust_eligible,
            reason="Wallet signature does not verify against the identity proof token.",
            verified_at=verified_at,
        )

    return SignedIdentityProofResult(
        valid=True,
        wallet_address=data.wallet_address,
        audience=data.audience,
        trust_state=token.trust_state,
        high_trust_eligible=token.high_trust_eligible,
        reason="Wallet signature proves control of a verified AadhaarChain identity token.",
        verified_at=verified_at,
    )


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
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


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
