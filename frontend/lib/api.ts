import axios, { AxiosInstance, AxiosError, InternalAxiosRequestConfig } from 'axios';
import type {
  Identity,
  CreateIdentityRequest,
  CreateIdentityResponse,
  AadhaarVerificationData,
  AgentRunProvenance,
  AgentToolTrace,
  ComplianceVerificationEvidence,
  DocumentVerificationEvidence,
  FraudVerificationEvidence,
  VerificationGap,
  VerificationMetadata,
  PanVerificationData,
  VerificationResponse,
  VerificationStatus,
  Credential,
  CredentialRequest,
  TransactionResponse,
  ApiResponse,
} from './types';

interface BackendIdentity {
  did: string;
  owner: string;
  commitment: string;
  verification_bitmap: number;
  created_at: string;
  updated_at: string;
}

interface BackendCreateIdentityResponse {
  identity: BackendIdentity;
  signature?: string;
}

interface BackendVerificationResponse {
  success: boolean;
  verification_id: string;
  status: VerificationResponse['status'];
  message: string;
}

interface BackendVerificationStatus {
  verification_id: string;
  status: VerificationStatus['status'];
  current_step?: VerificationStatus['currentStep'];
  progress: number;
  steps: VerificationStatus['steps'];
  metadata?: BackendVerificationMetadata;
  error?: string;
}

interface BackendVerificationGap {
  code: string;
  stage: VerificationGap['stage'];
  message: string;
  blocking: boolean;
}

interface BackendAgentToolTrace {
  tool_name: string;
  status: AgentToolTrace['status'];
  output_preview?: string;
}

interface BackendAgentRunProvenance {
  agent_id: string;
  status: AgentRunProvenance['status'];
  started_at: string;
  completed_at: string;
  model?: string;
  session_id?: string;
  tools: BackendAgentToolTrace[];
  response_preview?: string;
  structured_output?: Record<string, unknown>;
  error?: string;
}

interface BackendDocumentVerificationEvidence {
  document_type: DocumentVerificationEvidence['documentType'];
  input_kind: DocumentVerificationEvidence['inputKind'];
  extracted_fields: Record<string, unknown>;
  submitted_claims: Record<string, unknown>;
  confidence?: number;
  warnings: string[];
  required_fields: string[];
  missing_fields: string[];
  provenance: BackendAgentRunProvenance;
  gaps: BackendVerificationGap[];
}

interface BackendFraudVerificationEvidence {
  risk_score?: number;
  risk_level?: string;
  indicators: string[];
  recommendation?: FraudVerificationEvidence['recommendation'];
  provenance: BackendAgentRunProvenance;
  gaps: BackendVerificationGap[];
}

interface BackendComplianceVerificationEvidence {
  aadhaar_act_compliant?: boolean;
  dpdp_compliant?: boolean;
  violations: string[];
  recommendation?: ComplianceVerificationEvidence['recommendation'];
  provenance: BackendAgentRunProvenance;
  gaps: BackendVerificationGap[];
}

interface BackendVerificationMetadata {
  decision: VerificationMetadata['decision'];
  reason: string;
  evidence_status: VerificationMetadata['evidenceStatus'];
  document: BackendDocumentVerificationEvidence;
  fraud: BackendFraudVerificationEvidence;
  compliance: BackendComplianceVerificationEvidence;
  blocking_gaps: BackendVerificationGap[];
  assumptions: string[];
}

// API base URL from env or default
const API_BASE_URL = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000';

// Create axios instance
const apiClient: AxiosInstance = axios.create({
  baseURL: API_BASE_URL,
  timeout: 30000,
  headers: {
    'Content-Type': 'application/json',
  },
});

// Request interceptor - add auth token if available
apiClient.interceptors.request.use(
  (config: InternalAxiosRequestConfig) => {
    // Add wallet signature or JWT token here when available
    const token = localStorage.getItem('auth_token');
    if (token && config.headers) {
      config.headers.Authorization = `Bearer ${token}`;
    }
    return config;
  },
  (error) => Promise.reject(error)
);

// Response interceptor - handle errors globally
apiClient.interceptors.response.use(
  (response) => response,
  (error: AxiosError<ApiResponse<never>>) => {
    if (error.response) {
      // Server responded with error status
      console.error('API Error:', error.response.data);
    } else if (error.request) {
      // Request made but no response
      console.error('Network Error:', error.message);
    } else {
      // Error setting up request
      console.error('Request Error:', error.message);
    }
    return Promise.reject(error);
  }
);

// ===== IDENTITY MODULE =====
function toIdentity(identity: BackendIdentity): Identity {
  return {
    did: identity.did,
    owner: identity.owner,
    commitment: identity.commitment,
    verificationBitmap: identity.verification_bitmap,
    createdAt: identity.created_at,
    updatedAt: identity.updated_at,
  };
}

function toCreateIdentityResponse(
  response: BackendCreateIdentityResponse
): CreateIdentityResponse {
  return {
    identity: toIdentity(response.identity),
    signature: response.signature,
  };
}

function toVerificationResponse(
  response: BackendVerificationResponse
): VerificationResponse {
  return {
    success: response.success,
    verificationId: response.verification_id,
    status: response.status,
    message: response.message,
  };
}

function toVerificationGap(gap: BackendVerificationGap): VerificationGap {
  return {
    code: gap.code,
    stage: gap.stage,
    message: gap.message,
    blocking: gap.blocking,
  };
}

function toAgentToolTrace(tool: BackendAgentToolTrace): AgentToolTrace {
  return {
    toolName: tool.tool_name,
    status: tool.status,
    outputPreview: tool.output_preview,
  };
}

function toAgentRunProvenance(provenance: BackendAgentRunProvenance): AgentRunProvenance {
  return {
    agentId: provenance.agent_id,
    status: provenance.status,
    startedAt: provenance.started_at,
    completedAt: provenance.completed_at,
    model: provenance.model,
    sessionId: provenance.session_id,
    tools: provenance.tools.map(toAgentToolTrace),
    responsePreview: provenance.response_preview,
    structuredOutput: provenance.structured_output,
    error: provenance.error,
  };
}

function toDocumentVerificationEvidence(
  evidence: BackendDocumentVerificationEvidence
): DocumentVerificationEvidence {
  return {
    documentType: evidence.document_type,
    inputKind: evidence.input_kind,
    extractedFields: evidence.extracted_fields,
    submittedClaims: evidence.submitted_claims,
    confidence: evidence.confidence,
    warnings: evidence.warnings,
    requiredFields: evidence.required_fields,
    missingFields: evidence.missing_fields,
    provenance: toAgentRunProvenance(evidence.provenance),
    gaps: evidence.gaps.map(toVerificationGap),
  };
}

function toFraudVerificationEvidence(
  evidence: BackendFraudVerificationEvidence
): FraudVerificationEvidence {
  return {
    riskScore: evidence.risk_score,
    riskLevel: evidence.risk_level,
    indicators: evidence.indicators,
    recommendation: evidence.recommendation,
    provenance: toAgentRunProvenance(evidence.provenance),
    gaps: evidence.gaps.map(toVerificationGap),
  };
}

function toComplianceVerificationEvidence(
  evidence: BackendComplianceVerificationEvidence
): ComplianceVerificationEvidence {
  return {
    aadhaarActCompliant: evidence.aadhaar_act_compliant,
    dpdpCompliant: evidence.dpdp_compliant,
    violations: evidence.violations,
    recommendation: evidence.recommendation,
    provenance: toAgentRunProvenance(evidence.provenance),
    gaps: evidence.gaps.map(toVerificationGap),
  };
}

function toVerificationMetadata(metadata: BackendVerificationMetadata): VerificationMetadata {
  return {
    decision: metadata.decision,
    reason: metadata.reason,
    evidenceStatus: metadata.evidence_status,
    document: toDocumentVerificationEvidence(metadata.document),
    fraud: toFraudVerificationEvidence(metadata.fraud),
    compliance: toComplianceVerificationEvidence(metadata.compliance),
    blockingGaps: metadata.blocking_gaps.map(toVerificationGap),
    assumptions: metadata.assumptions,
  };
}

function toVerificationStatus(
  status: BackendVerificationStatus
): VerificationStatus {
  return {
    verificationId: status.verification_id,
    status: status.status,
    currentStep: status.current_step,
    progress: status.progress,
    steps: status.steps,
    decision: status.metadata?.decision,
    metadata: status.metadata ? toVerificationMetadata(status.metadata) : undefined,
    error: status.error,
  };
}

export const identityApi = {
  // Get identity by wallet address
  async getIdentity(walletAddress: string): Promise<Identity> {
    const { data } = await apiClient.get<ApiResponse<BackendIdentity>>(
      `/api/identity/${walletAddress}`
    );
    if (!data.data) throw new Error(data.error?.message || 'Failed to fetch identity');
    return toIdentity(data.data);
  },

  // Create new identity
  async createIdentity(
    walletAddress: string,
    request: CreateIdentityRequest
  ): Promise<CreateIdentityResponse> {
    const { data } = await apiClient.post<ApiResponse<BackendCreateIdentityResponse>>(
      `/api/identity/${walletAddress}`,
      request
    );
    if (!data.data) throw new Error(data.error?.message || 'Failed to create identity');
    return toCreateIdentityResponse(data.data);
  },

  // Update identity commitment
  async updateCommitment(
    walletAddress: string,
    commitment: string
  ): Promise<Identity> {
    const { data } = await apiClient.patch<ApiResponse<BackendIdentity>>(
      `/api/identity/${walletAddress}`,
      { commitment }
    );
    if (!data.data) throw new Error(data.error?.message || 'Failed to update commitment');
    return toIdentity(data.data);
  },
};

// ===== VERIFICATION MODULE =====
export const verificationApi = {
  // Submit Aadhaar verification
  async submitAadhaar(
    walletAddress: string,
    verificationData: AadhaarVerificationData
  ): Promise<VerificationResponse> {
    const { data } = await apiClient.post<ApiResponse<BackendVerificationResponse>>(
      `/api/identity/${walletAddress}/aadhaar`,
      {
        name: verificationData.name,
        dob: verificationData.dob,
        uid: verificationData.uid,
        address: verificationData.address,
        document_hash: verificationData.documentHash,
        consent_provided: verificationData.consentProvided,
      }
    );
    if (!data.data) throw new Error(data.error?.message || 'Failed to submit verification');
    return toVerificationResponse(data.data);
  },

  // Submit PAN verification
  async submitPan(
    walletAddress: string,
    verificationData: PanVerificationData
  ): Promise<VerificationResponse> {
    const { data } = await apiClient.post<ApiResponse<BackendVerificationResponse>>(
      `/api/identity/${walletAddress}/pan`,
      {
        name: verificationData.name,
        pan_number: verificationData.panNumber,
        dob: verificationData.dob,
        document_hash: verificationData.documentHash,
      }
    );
    if (!data.data) throw new Error(data.error?.message || 'Failed to submit verification');
    return toVerificationResponse(data.data);
  },

  // Get verification status
  async getStatus(verificationId: string): Promise<VerificationStatus> {
    const { data } = await apiClient.get<ApiResponse<BackendVerificationStatus>>(
      `/api/identity/status/${verificationId}`
    );
    if (!data.data) throw new Error(data.error?.message || 'Failed to fetch status');
    return toVerificationStatus(data.data);
  },
};

// ===== CREDENTIALS MODULE =====
export const credentialsApi = {
  // Get all credentials for wallet
  async getCredentials(walletAddress: string): Promise<Credential[]> {
    const { data } = await apiClient.get<ApiResponse<Credential[]>>(
      `/api/credentials/${walletAddress}`
    );
    if (!data.data) throw new Error(data.error?.message || 'Failed to fetch credentials');
    return data.data;
  },

  // Issue new credential
  async issueCredential(
    walletAddress: string,
    request: CredentialRequest
  ): Promise<Credential> {
    const { data } = await apiClient.post<ApiResponse<Credential>>(
      `/api/credentials/${walletAddress}`,
      request
    );
    if (!data.data) throw new Error(data.error?.message || 'Failed to issue credential');
    return data.data;
  },

  // Revoke credential
  async revokeCredential(
    walletAddress: string,
    credentialId: string
  ): Promise<{ revoked: boolean }> {
    const { data } = await apiClient.delete<ApiResponse<{ revoked: boolean }>>(
      `/api/credentials/${walletAddress}/${credentialId}`
    );
    if (!data.data) throw new Error(data.error?.message || 'Failed to revoke credential');
    return data.data;
  },
};

// ===== TRANSACTION MODULE =====
export const transactionApi = {
  // Prepare unsigned transaction
  async prepareTransaction(
    walletAddress: string,
    instruction: string,
    params: Record<string, unknown>
  ): Promise<{ transaction: string }> {
    const { data } = await apiClient.post<ApiResponse<{ transaction: string }>>(
      `/api/transaction/prepare`,
      { walletAddress, instruction, params }
    );
    if (!data.data) throw new Error(data.error?.message || 'Failed to prepare transaction');
    return data.data;
  },

  // Submit signed transaction
  async submitTransaction(signedTransaction: string): Promise<TransactionResponse> {
    const { data } = await apiClient.post<ApiResponse<TransactionResponse>>(
      '/api/transaction/submit',
      { transaction: signedTransaction }
    );
    if (!data.data) throw new Error(data.error?.message || 'Failed to submit transaction');
    return data.data;
  },
};

// Export the api client for direct use if needed
export default apiClient;

// Helper to check if wallet address is valid
export function isValidPublicKey(address: string): boolean {
  try {
    // Base58 check for Solana addresses (typically 32-44 chars)
    return /^[1-9A-HJ-NP-Za-km-z]{32,44}$/.test(address);
  } catch {
    return false;
  }
}
