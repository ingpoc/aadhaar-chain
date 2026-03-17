"""Pydantic models for identity, verification, and credentials."""
from pydantic import BaseModel, Field, validator
from typing import Optional, List, Literal, Dict, Any
from enum import Enum


# --- Base Models ---


class ApiResponse(BaseModel):
    """Wrapper for consistent API responses."""
    success: bool = True
    message: str = ""
    data: Optional[Dict[str, Any]] = None
    error: Optional[Dict[str, Any]] = None


class ApiError(BaseModel):
    """API error details."""
    message: str
    code: Optional[str] = None
    details: Optional[Any] = None


# --- Verification Models ---


class VerificationStep(str, Enum):
    """Verification workflow steps."""
    document_received = "document_received"
    parsing = "parsing"
    fraud_check = "fraud_check"
    compliance_check = "compliance_check"
    blockchain_upload = "blockchain_upload"
    complete = "complete"


class StepStatus(str, Enum):
    """Status of individual verification step."""
    pending = "pending"
    in_progress = "in_progress"
    completed = "completed"
    failed = "failed"


class VerificationStepDetail(BaseModel):
    """Detailed verification step with status."""
    name: str
    status: StepStatus


class VerificationGap(BaseModel):
    """Explicit missing evidence or provenance in the verification chain."""
    code: str
    stage: Literal["document", "fraud", "compliance", "decision"]
    message: str
    blocking: bool = True


class AgentToolTrace(BaseModel):
    """Observed tool usage during an agent run."""
    tool_name: str
    status: Literal["requested", "completed", "failed"]
    output_preview: Optional[str] = None


class AgentRunProvenance(BaseModel):
    """Stable provenance contract for each agent invocation."""
    agent_id: str
    status: Literal["completed", "missing_contract", "failed"]
    started_at: str
    completed_at: str
    model: Optional[str] = None
    session_id: Optional[str] = None
    tools: List[AgentToolTrace] = Field(default_factory=list)
    response_preview: Optional[str] = None
    structured_output: Optional[Dict[str, Any]] = None
    error: Optional[str] = None


class DocumentEvidenceSource(BaseModel):
    """Descriptor for the primary document evidence observed by the backend."""
    transport: Literal["upload", "request_payload", "unknown"]
    file_name: Optional[str] = None
    content_type: Optional[str] = None
    size_bytes: Optional[int] = None
    sha256: Optional[str] = None
    submitted_hash: Optional[str] = None
    hash_matches_submission: Optional[bool] = None


class DocumentVerificationEvidence(BaseModel):
    """Evidence contract for the document parsing stage."""
    document_type: Literal["aadhaar", "pan"]
    input_kind: Literal["raw_document", "request_payload", "unknown"]
    source: Optional[DocumentEvidenceSource] = None
    extracted_fields: Dict[str, Any] = Field(default_factory=dict)
    submitted_claims: Dict[str, Any] = Field(default_factory=dict)
    confidence: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    warnings: List[str] = Field(default_factory=list)
    required_fields: List[str] = Field(default_factory=list)
    missing_fields: List[str] = Field(default_factory=list)
    provenance: AgentRunProvenance
    gaps: List[VerificationGap] = Field(default_factory=list)


class FraudVerificationEvidence(BaseModel):
    """Evidence contract for the fraud stage."""
    risk_score: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    risk_level: Optional[str] = None
    indicators: List[str] = Field(default_factory=list)
    recommendation: Optional[Literal["approve", "manual_review", "block"]] = None
    provenance: AgentRunProvenance
    gaps: List[VerificationGap] = Field(default_factory=list)


class ComplianceVerificationEvidence(BaseModel):
    """Evidence contract for the compliance stage."""
    aadhaar_act_compliant: Optional[bool] = None
    dpdp_compliant: Optional[bool] = None
    violations: List[str] = Field(default_factory=list)
    recommendation: Optional[Literal["approve", "manual_review", "block"]] = None
    provenance: AgentRunProvenance
    gaps: List[VerificationGap] = Field(default_factory=list)


class VerificationMetadata(BaseModel):
    """Typed verification metadata returned to the frontend."""
    decision: Literal["approve", "reject", "manual_review"]
    reason: str
    evidence_status: Literal["complete", "partial", "missing"]
    document: DocumentVerificationEvidence
    fraud: FraudVerificationEvidence
    compliance: ComplianceVerificationEvidence
    blocking_gaps: List[VerificationGap] = Field(default_factory=list)
    assumptions: List[str] = Field(default_factory=list)


class ConsentArtifact(BaseModel):
    """Public consent summary exposed to trust consumers."""
    status: Literal["pending", "granted", "missing", "not_required"]
    scope: Optional[str] = None
    purpose: Optional[str] = None
    reference: Optional[str] = None


class AttestationArtifact(BaseModel):
    """Stable trust artifact for downstream credential/attestation consumers."""
    status: Literal["pending", "not_issued", "issued", "revoked"]
    credential_type: str
    reference: Optional[str] = None


class RevocationArtifact(BaseModel):
    """Revocation status for any downstream trust artifact."""
    status: Literal["pending", "not_applicable", "active", "revoked"]
    reference: Optional[str] = None


class ReviewArtifact(BaseModel):
    """Manual review or approval status without exposing internal evidence payloads."""
    status: Literal["pending", "manual_review_required", "approved", "rejected"]
    reference: Optional[str] = None
    reason: Optional[str] = None


class AuditReceiptReference(BaseModel):
    """Reference to a durable backend trust or audit record."""
    kind: Literal["verification_record", "decision_record", "consent_record"]
    reference: str
    created_at: str


class TrustVerificationSummary(BaseModel):
    """Downstream-safe trust view for one verification workflow."""
    document_type: Literal["aadhaar", "pan"]
    verification_id: str
    workflow_status: Literal["pending", "processing", "verified", "failed", "manual_review"]
    decision: Optional[Literal["approve", "reject", "manual_review"]] = None
    reason: Optional[str] = None
    evidence_status: Optional[Literal["complete", "partial", "missing"]] = None
    consent: ConsentArtifact
    attestation: AttestationArtifact
    revocation: RevocationArtifact
    review: ReviewArtifact
    audit_receipts: List[AuditReceiptReference] = Field(default_factory=list)


class TrustReadSurface(BaseModel):
    """Stable public trust contract for downstream portfolio consumers."""
    trust_version: Literal["v1"] = "v1"
    wallet_address: str
    did: str
    verification_bitmap: int = 0
    updated_at: str
    trust_state: Literal[
        "identity_present_unverified",
        "verified",
        "manual_review",
        "revoked_or_blocked",
    ]
    high_trust_eligible: bool = False
    state_reason: Optional[str] = None
    verifications: List[TrustVerificationSummary] = Field(default_factory=list)


class VerificationStatus(BaseModel):
    """Status of verification process."""
    verification_id: str
    wallet_address: str
    status: Literal["pending", "processing", "verified", "failed", "manual_review"]
    current_step: Optional[VerificationStep] = None
    steps: List[VerificationStepDetail] = Field(default_factory=list)
    progress: float = Field(default=0.0, ge=0.0, le=1.0)
    created_at: str
    updated_at: str
    error: Optional[str] = None
    metadata: Optional[VerificationMetadata] = None


# --- Identity Models ---


class IdentityData(BaseModel):
    """Identity data on blockchain - matches frontend Identity interface."""
    did: str  # Decentralized Identifier
    owner: str  # Wallet address that owns this identity
    commitment: str  # Hash commitment on chain
    verification_bitmap: int = Field(default=0, ge=0)  # Bitmask of verified credential types
    created_at: str  # ISO timestamp
    updated_at: str  # ISO timestamp


class CreateIdentityRequest(BaseModel):
    """Request to create a new identity."""
    commitment: str


class CreateIdentityResponse(BaseModel):
    """Response after creating identity."""
    identity: IdentityData
    signature: Optional[str] = None


class UpdateIdentityRequest(BaseModel):
    """Request to update mutable identity fields."""
    commitment: Optional[str] = None
    verification_bitmap: Optional[int] = Field(default=None, ge=0)


class TrustFixtureRequest(BaseModel):
    """Local development request for seeding deterministic trust states."""
    fixture_state: Literal[
        "no_identity",
        "identity_present_unverified",
        "verified",
        "manual_review",
        "revoked_or_blocked",
    ]
    document_type: Literal["aadhaar", "pan"] = "aadhaar"


# --- Verification Request Models ---


class AadhaarVerificationData(BaseModel):
    """Aadhaar card verification request."""
    name: str
    dob: str
    uid: str
    address: Optional[str] = None
    document_hash: Optional[str] = None
    consent_provided: bool = False


class PanVerificationData(BaseModel):
    """PAN card verification request."""
    name: str
    pan_number: str = Field(min_length=10, max_length=10, pattern=r"^[A-Z]{5}[0-9]{4}[A-Z]$")
    dob: str
    document_hash: Optional[str] = None


class VerificationRequest(BaseModel):
    """Generic verification request."""
    document_type: Literal["aadhaar", "pan"]
    document_data: str  # Base64 encoded or hash


class VerificationResponse(BaseModel):
    """Response after initiating verification."""
    success: bool
    verification_id: str
    status: Literal["document_received", "pending", "processing", "verified", "failed", "manual_review"]
    message: str


# --- Credential Models ---


class CredentialClaim(BaseModel):
    """Claim on a verifiable credential - flexible key-value structure."""
    claim_type: str  # e.g., "name", "age", "nationality"
    claim_value: str  # The value of the claim
    verified_at: Optional[str] = None  # When this claim was verified


class Credential(BaseModel):
    """Verifiable credential - matches frontend Credential interface."""
    id: str  # Credential identifier
    type: str  # e.g., "aadhaar", "pan", "driving_license"
    issuer: str  # DID of the issuer
    subject: str  # DID of the subject (user)
    issuance_date: int  # Unix timestamp
    expiration_date: Optional[int] = None  # Unix timestamp
    revoked: bool = False
    claims: Dict[str, Any]  # Flexible claims structure


class CredentialRequest(BaseModel):
    """Request to issue a new credential."""
    type: str
    claims: Dict[str, Any]


# --- Wallet Models ---


class WalletBalance(BaseModel):
    """Wallet SOL balance."""
    lamports: int
    sol: float


# --- Transaction Models ---


class TransactionData(BaseModel):
    """Transaction for identity/asset operations."""
    transaction_id: str
    wallet_address: str
    transaction_type: Literal["identity_create", "credential_issue", "identity_update", "credential_revoke"]
    amount: Optional[int] = None
    status: Literal["pending", "confirmed", "failed"]
    created_at: str
    signature: Optional[str] = None  # Solana transaction signature


class TransactionResponse(BaseModel):
    """Response after transaction submission."""
    signature: str
    success: bool
    error: Optional[str] = None


# --- Transaction Request Models ---


class PrepareTransactionRequest(BaseModel):
    """Request to prepare a transaction (unsigned)."""
    wallet_address: str
    transaction_type: Literal["identity_create", "credential_issue", "identity_update", "credential_revoke"]
    data: Optional[Dict[str, Any]] = None


class SubmitTransactionRequest(BaseModel):
    """Request to submit a signed transaction."""
    wallet_address: str
    signature: str
    transaction_type: str


# --- Additional Helper Models ---


class StatusUpdate(BaseModel):
    """Generic status update."""
    verification_id: str
    status: Literal["pending", "processing", "verified", "failed", "manual_review"]
    progress: Optional[float] = Field(None, ge=0.0, le=1.0)
    error: Optional[str] = None
