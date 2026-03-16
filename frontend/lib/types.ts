// API Request/Response Types

export interface PublicKey {
  toBase58(): string;
}

// Identity Types
export interface Identity {
  did: string;
  owner: string;
  commitment: string;
  verificationBitmap: number;
  createdAt: string;
  updatedAt: string;
}

export interface CreateIdentityRequest {
  commitment: string;
}

export interface CreateIdentityResponse {
  identity: Identity;
  signature?: string;
}

// Verification Types
export interface VerificationRequest {
  documentType: 'aadhaar' | 'pan';
  documentData: string | File;
}

export interface AadhaarVerificationData {
  name: string;
  dob: string;
  uid: string;
  address?: string;
  documentHash?: string;
  consentProvided: boolean;
}

export interface PanVerificationData {
  name: string;
  panNumber: string;
  dob: string;
  documentHash?: string;
}

export interface VerificationResponse {
  success: boolean;
  verificationId: string;
  status: 'document_received' | 'pending' | 'processing' | 'verified' | 'failed' | 'manual_review';
  message: string;
}

export interface VerificationGap {
  code: string;
  stage: 'document' | 'fraud' | 'compliance' | 'decision';
  message: string;
  blocking: boolean;
}

export interface AgentToolTrace {
  toolName: string;
  status: 'requested' | 'completed' | 'failed';
  outputPreview?: string;
}

export interface AgentRunProvenance {
  agentId: string;
  status: 'completed' | 'missing_contract' | 'failed';
  startedAt: string;
  completedAt: string;
  model?: string;
  sessionId?: string;
  tools: AgentToolTrace[];
  responsePreview?: string;
  structuredOutput?: Record<string, unknown>;
  error?: string;
}

export interface DocumentVerificationEvidence {
  documentType: 'aadhaar' | 'pan';
  inputKind: 'raw_document' | 'request_payload' | 'unknown';
  extractedFields: Record<string, unknown>;
  submittedClaims: Record<string, unknown>;
  confidence?: number;
  warnings: string[];
  requiredFields: string[];
  missingFields: string[];
  provenance: AgentRunProvenance;
  gaps: VerificationGap[];
}

export interface FraudVerificationEvidence {
  riskScore?: number;
  riskLevel?: string;
  indicators: string[];
  recommendation?: 'approve' | 'manual_review' | 'block';
  provenance: AgentRunProvenance;
  gaps: VerificationGap[];
}

export interface ComplianceVerificationEvidence {
  aadhaarActCompliant?: boolean;
  dpdpCompliant?: boolean;
  violations: string[];
  recommendation?: 'approve' | 'manual_review' | 'block';
  provenance: AgentRunProvenance;
  gaps: VerificationGap[];
}

export interface VerificationMetadata {
  decision: 'approve' | 'reject' | 'manual_review';
  reason: string;
  evidenceStatus: 'complete' | 'partial' | 'missing';
  document: DocumentVerificationEvidence;
  fraud: FraudVerificationEvidence;
  compliance: ComplianceVerificationEvidence;
  blockingGaps: VerificationGap[];
  assumptions: string[];
}

export interface VerificationStatus {
  verificationId: string;
  status: 'pending' | 'processing' | 'verified' | 'failed' | 'manual_review';
  currentStep?: 'document_received' | 'parsing' | 'fraud_check' | 'compliance_check' | 'blockchain_upload' | 'complete';
  progress: number;
  steps: {
    name: string;
    status: 'pending' | 'in_progress' | 'completed' | 'failed';
  }[];
  metadata?: VerificationMetadata;
  decision?: 'approve' | 'reject' | 'manual_review';
  error?: string;
}

// Credential Types
export interface Credential {
  id: string;
  type: string;
  issuer: string;
  subject: string;
  issuanceDate: number;
  expirationDate: number;
  revoked: boolean;
  claims: Record<string, unknown>;
}

export interface CredentialRequest {
  type: string;
  claims: Record<string, unknown>;
}

// Wallet Types
export interface WalletBalance {
  lamports: number;
  sol: number;
}

export interface TransactionResponse {
  signature: string;
  success: boolean;
  error?: string;
}

// API Error Types
export interface ApiError {
  message: string;
  code?: string;
  details?: unknown;
}

export interface ApiResponse<T> {
  success: boolean;
  data?: T;
  error?: ApiError;
}
