# AGENTS.md

## Scope

Repo-local guidance for `aadhaar-chain` only.

**Portfolio QA / browser / same-wallet control owner:** `qa/docs/workflow/`  
Entry: `qa/docs/workflow/README.md` → then control plane → acceptance loop.

There is no parent `../AGENTS.md` in this multi-repo checkout. Do not invent one. Do not create parallel portfolio workflow docs elsewhere.

## Portfolio testing (pointer only)

- BEFORE browser testing → `qa/docs/workflow/browser-testing-control-plane.md`
- BEFORE same-wallet journey → `qa/docs/workflow/portfolio-browser-acceptance-loop.md`
- Session friction → `qa/docs/workflow/session-friction-log.md`
- Runners: `qa/` only (`grade:deterministic`, `grade:browser`, `grade:wallet`)
- AadhaarChain is the first browser checkpoint; downstream trust consumers depend on its identity/trust state
- Critical routes: `/`, `/dashboard`, `/identity/create`, `/verify/aadhaar`, `/verify/pan`, `/credentials`, `/settings`
- Confirm frontend (43100) + gateway (43101) healthy before product conclusions

## Agent SDK Integration

### Purpose
Manage Codex Agent SDK integration for aadhaar-chain verification workflows.

### Components

#### Agent Manager (`gateway/app/agent_manager.py`)
**Service:** AgentManager  
**Purpose:** Orchestrates agent invocations and manages verification workflows

**Methods:**
- `initialize_agents()` - Initialize Codex Agent SDK and MCP servers
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

#### Verification Routes (`gateway/app/routes.py`)
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

#### Gateway (`gateway/main.py`)
**Startup:**
- Initialize Codex Agent SDK
- Connect to MCP servers (document-processor, pattern-analyzer, compliance-rules)
- Load agent definitions

**Health Check:** `/health`
- Returns service status, version, and health status

## Testing

### Test Suite (`gateway/tests/test_agent_manager.py`)
**Run:**
```bash
cd gateway
pytest tests/test_agent_manager.py -v
```

## Notes

Current agent results are mock (OCR, fraud, compliance). feat-011 targets real Codex Agent SDK + MCP. Prefer Redis/PostgreSQL over in-memory verification records for production.
