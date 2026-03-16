"""Routes for identity operations with agent integration."""
from fastapi import APIRouter, BackgroundTasks, HTTPException
from typing import Optional

from app.models import (
    IdentityData,
    CreateIdentityRequest,
    CreateIdentityResponse,
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
    data: AadhaarVerificationData
):
    """Create Aadhaar card verification request and start agent workflow."""
    verification_id = await agent_manager.create_verification(
        wallet_address,
        "aadhaar",
        data
    )
    background_tasks.add_task(
        agent_manager.orchestrate_verification,
        wallet_address,
        "aadhaar",
        data.model_dump_json().encode("utf-8"),
        data,
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
    data: PanVerificationData
):
    """Create PAN card verification request and start agent workflow."""
    verification_id = await agent_manager.create_verification(
        wallet_address,
        "pan",
        data
    )
    background_tasks.add_task(
        agent_manager.orchestrate_verification,
        wallet_address,
        "pan",
        data.model_dump_json().encode("utf-8"),
        data,
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
