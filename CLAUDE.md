# Agent SDK Integration

## Purpose
Manage Claude Agent SDK integration for aadhaar-chain verification workflows.

## Components

### Agent Manager (`gateway/app/agent_manager.py`)
**Service:** AgentManager
**Purpose:** Orchestrates agent invocations and manages verification workflows

**Methods:**
- `initialize_agents()` - Initialize Claude Agent SDK and MCP servers
- `validate_document()` - Call Document Validator agent (OCR, field extraction)
- `detect_fraud()` - Call Fraud Detection agent (risk scoring, tampering checks)
- `check_compliance()` - Call Compliance Monitor agent (Aadhaar Act, DPDP Act)
- `orchestrate_verification()` - Full workflow orchestration (parse → fraud → compliance → decision)
- `get_verification_status()` - Get verification status by ID
- `create_verification()` - Create new verification request
- `update_verification_progress()` - Update progress (0.0-1.0)
- `complete_verification()` - Mark complete with decision

**Data Models:**
- `AgentType` enum - DOCUMENT_VALIDATOR, FRAUD_DETECTION, COMPLIANCE_MONITOR, ORCHESTRATOR
- `AgentTask` - Task tracking with created_at, completed_at, result, error

**Workflow:**
```
1. Document Upload → Validate Document (Document Validator)
2. Fraud Check → Detect Tampering (Fraud Detection)
3. Compliance Check → Legal Validation (Compliance Monitor)
4. Decision → Approve, Reject, or Manual Review (Orchestrator)
```

**Decision Logic:**
- Risk score > 0.7 → REJECT
- Not Aadhaar Act or DPDP compliant → REJECT
- OCR confidence < 0.6 → MANUAL REVIEW
- Otherwise → APPROVE

### Verification Routes (`gateway/app/routes.py`)
**Base Path:** `/api/identity`

**Endpoints:**
- `POST /{wallet_address}/aadhaar` - Create Aadhaar verification
- `POST /{wallet_address}/pan` - Create PAN verification
- `GET /status/{verification_id}` - Get verification status
- `GET /{wallet_address}` - Get identity data
- `POST /{wallet_address}` - Update identity data
- `POST /verify/aadhaar` - Verify Aadhaar with full workflow
- `POST /verify/pan` - Verify PAN with full workflow

**Features:**
- Agent manager integration
- Progress tracking with steps
- Decision storage in verification metadata
- In-memory verification records store

### Gateway (`gateway/main.py`)
**Startup:**
- Initialize Claude Agent SDK
- Connect to MCP servers (document-processor, pattern-analyzer, compliance-rules)
- Load agent definitions

**Health Check:** `/health`
- Returns service status, version, and health status

## Testing

### Test Suite (`gateway/tests/test_agent_manager.py`)
**Tests:**
- `test_create_verification` - Create new verification request
- `test_get_verification_status` - Get status by ID
- `test_validate_document_mock` - Document validation (mock)
- `test_detect_fraud_mock` - Fraud detection (mock)
- `test_check_compliance_mock` - Compliance check (mock)
- `test_orchestrate_verification_mock` - Full workflow (mock)
- `test_update_verification_progress` - Progress tracking
- `test_cleanup_expired_verifications` - Record cleanup
- `test_agent_initialization` - Agent setup verification

**Run Tests:**
```bash
cd gateway
pytest tests/test_agent_manager.py -v
```

## Next Steps

### feat-011: Orchestrator Agent Workflow
**Goal:** Implement actual agent-to-agent coordination using Task tool

**Tasks:**
1. Setup Claude Agent SDK (API keys, MCP server connections)
2. Create agent instances from `mcp/agents.py`
3. Implement Task tool invocations
4. Add error handling for agent failures
5. Store decision traces in Context Graph
6. Replace mock implementations with real agent calls

**Success Criteria:**
- Can invoke Document Validator agent
- Can invoke Fraud Detection agent
- Can invoke Compliance Monitor agent
- Can use Task tool for subagent coordination
- Can handle agent errors gracefully
- Can store decisions in Context Graph

## Current Status

**Completed:**
- ✅ Agent manager service (mock implementations)
- ✅ Verification routes with agent integration
- ✅ Test suite
- ✅ Decision logic (approve/reject/manual review)
- ✅ Progress tracking

**Pending (feat-011):**
- Real Claude Agent SDK integration
- MCP server connections
- Actual agent invocations
- Context Graph integration

## Notes

### Mock vs Real Implementation
Current implementation uses mock data for agent results (OCR, fraud, compliance). Next feature (feat-011) will integrate real Claude Agent SDK and MCP server calls.

### Performance Considerations
- In-memory verification records (for development)
- Production: Use Redis or PostgreSQL
- Agent initialization: Cache instances (avoid re-initializing)
- MCP connections: Reuse connections across invocations

---

**Last Updated:** 2026-02-03 17:10 UTC
