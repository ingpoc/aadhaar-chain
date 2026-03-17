# aadhaar-chain - Memory & Decision Log

## 🎯 Project Vision
Identity verification platform using Solana blockchain + Claude Agent SDK + MCP servers
- Verified credentials on-chain (DID, verification bitmap)
- OCR-based document parsing (Aadhaar, PAN)
- Fraud detection with compliance rules (Aadhaar Act 2019, DPDP Act 2019)
- Context Graph for decision traces and precedents

## 📊 Current Progress

### ✅ Completed Features (13/22 - 59%)
- Frontend (Phase 1-4): 11 features (50%)
- Backend + Agent SDK (Phase 5): 2 features (18%)

### 🏗️ Currently Working On
- Phase 5 (Backend + Agent SDK)
- Just completed: feat-009 (Pattern Analyzer + Compliance Rules MCP tools)

### 📋 Next Features (Pending)
- feat-010: Agent definitions - **IN PROGRESS** (partially done)
- feat-011: Orchestrator agent workflow
- feat-012: Verification routes with status tracking
- feat-013: Identity and Transaction routes
- feat-014: Credentials routes

## 💡 Important Decisions Made

### 2026-03-17 Verification Trust Contract
1. **Do not fabricate backend verification outputs**
   - Decision: Removed placeholder OCR/fraud/compliance results from `gateway/app/agent_manager.py`.
   - Reasoning: Backend-driven status is not trustworthy if it promotes missing agent output into fake approvals.
   - Outcome: Verification now records typed evidence, provenance, and explicit blocking gaps.

2. **Manual review is the default for missing primary evidence**
   - Decision: If the gateway only receives self-asserted request payload fields instead of raw document bytes, the verification terminates as `manual_review`.
   - Reasoning: Submitted form data is not equivalent to observed document evidence.
   - Outcome: Current Aadhaar/PAN flows surface `primary_document_missing` until a real upload/evidence path is wired through.

3. **Approval requires complete contracts from all stages**
   - Decision: Auto-approval now requires explicit structured contracts for document parsing, fraud analysis, and compliance.
   - Reasoning: Missing provenance or unstructured agent replies must never silently downgrade into “safe”.
   - Outcome: `VerificationMetadata` now carries `document`, `fraud`, `compliance`, `blocking_gaps`, and `evidence_status`.

4. **Runtime gaps found during verification hardening**
   - `ApiResponse.message` needed a default because several routes construct responses without one.
   - `mcp/agents.py` was missing `List` import.
   - Local `mcp/` needed package-safe loading because the top-level `mcp` name can collide with installed packages.
   - `pytest` required `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1` in this environment because a globally loaded `anchorpy` plugin depends on `pytest_xprocess`.

### 2026-03-17 Primary Evidence Ingestion
1. **Verification requests must upload primary document bytes**
   - Decision: Switched Aadhaar and PAN verification submission from JSON-only requests to multipart uploads carrying the file plus structured claims.
   - Reasoning: The backend trust contract cannot distinguish real evidence from self-asserted claims if the uploaded document never crosses the API boundary.
   - Outcome: `gateway/app/routes.py` now reads uploaded files, computes source metadata, and passes raw bytes into `agent_manager.orchestrate_verification`.

2. **Backend computes the evidence fingerprint**
   - Decision: The gateway now computes a SHA-256 digest and records file name, content type, size, and optional submitted-hash match status in the document evidence contract.
   - Reasoning: Trustworthy provenance should be based on observed backend input, not only on a client-provided hash field.
   - Outcome: `VerificationMetadata.document.source` exposes stable upload provenance to both tests and the UI.

3. **Successful ingestion still allows manual review**
   - Decision: Browser validation now treats `raw_document` plus upload provenance as the success criterion for ingestion, even when the downstream document agent still returns `missing_contract`.
   - Reasoning: Evidence transport and agent extraction are separate concerns; this branch fixes the former and makes the remaining agent gap explicit.
   - Outcome: Aadhaar and PAN both render `Document input: raw_document` with upload metadata and computed SHA-256 in the live browser while still falling back to `manual_review` for missing structured extraction output.

### 2026-03-17 Document Validator Contract Recovery
1. **Normalize MCP server identifiers at the gateway boundary**
   - Decision: `agent_manager._build_mcp_servers()` now accepts both plain server keys and `mcp://...` identifiers.
   - Reasoning: The agent definitions were using `mcp://document-processor` style names while the gateway registry used plain keys, which silently disabled tool attachment.
   - Outcome: Document, fraud, and compliance agents can be configured without depending on one identifier style.

2. **Document processor must be launchable and deterministic**
   - Decision: Replaced the broken `mcp-servers/document-processor` package stub with a real `server.py` FastMCP entrypoint and deterministic extraction helpers.
   - Reasoning: The previous package had no runnable `server.py` and even its `__init__.py` had syntax errors, so the configured MCP server path could never succeed.
   - Outcome: The configured command now points at an actual server implementation, and the document-processing contract is explicit in code.

3. **Document parsing cannot block indefinitely on the agent runtime**
   - Decision: `validate_document()` now applies a timeout to the document-validator agent path and falls back to deterministic local extraction with explicit provenance (`model=\"deterministic-fallback\"`) when the agent contract is unavailable.
   - Reasoning: Trustworthy verification still requires a structured document contract even when the agent runtime hangs or fails to return JSON.
   - Outcome: Browser and API validation now reach a final status with `Document agent: completed` instead of hanging in `parsing`, while still landing in `manual_review` when required fields are absent.

### Architecture Decisions
1. **Tech Stack**
   - Frontend: Next.js 15, TypeScript, Tailwind CSS, shadcn/ui
   - Backend: FastAPI, Uvicorn, Pydantic
   - Agents: Claude Agent SDK (Anthropic)
   - Blockchain: Solana (solders)
   - MCP: Document Processor, Pattern Analyzer, Compliance Rules

2. **MCP Server Structure**
   - Decision: Separate MCP servers for specialized tasks
   - Reasoning: Better isolation, can scale individually
   - Storage: `mcp-servers/` directory structure
   - Files created:
     - `document-processor/__init__.py`
     - `pattern-analyzer/__init__.py`
     - `compliance-rules/__init__.py`

3. **Agent Architecture**
   - Decision: 4 specialized agents + 1 orchestrator
   - Agents: Document Validator, Fraud Detection, Compliance Monitor, Orchestrator
   - Tool restrictions: Subagents DON'T have Task tool (prevents infinite loops)
   - Orchestrator: HAS Task tool (can coordinate workflows)
   - Storage: `mcp/agents.py` with AgentDefinition class

4. **Context Graph Usage**
   - Decision: Store ALL verification decisions as traces
   - Benefits: Query precedents, learn from past decisions
   - Categories: architecture, fraud-detection, compliance-monitor, verification
   - MCP Tools: `mcp_context_store_trace`, `mcp_context_query_traces`

5. **Fraud Detection Approach**
   - Decision: Multi-layered (OCR quality + tampering + compliance)
   - Risk scoring: 0.0-1.0 scale
   - Categories: Safe (0-0.2), Low (0.2-0.5), Medium (0.5-0.7), High (0.7-1.0)
   - Outcome mapping:
     - Risk < 0.7 + Compliant + OCR > 0.6 → APPROVE
     - Risk > 0.7 OR Violation → REJECT
     - Otherwise → MANUAL_REVIEW

6. **Compliance Rules**
   - Aadhaar Act 2019:
     - Purpose limitation (KYC only)
     - Explicit consent required
     - Data minimization (only essential fields)
   - DPDP Act 2019:
     - Data minimization (collect only what's needed)
     - Storage duration (X days default)
     - Access control (role-based)

### Context Graph Queries Used
- Query: "FastAPI gateway setup backend scaffolding configuration health endpoint"
  - Category: architecture
  - Outcome: success
  - Decision: Use FastAPI (async, modern) over Flask

- Query: "Document Processor MCP tools Tesseract OCR"
  - Category: architecture
  - Outcome: success
  - Decision: Use Python OCR with Tesseract integration

### Data Model Decisions
1. **Verification Status**
   - Steps: document_received, parsing, fraud_check, compliance_check, blockchain_upload, complete
   - Progress: 0.0-1.0 float
   - State management: In-memory store (for development)

2. **Credential Model**
   - Structure: CredentialClaim (type, value, verified_at) on Credential
   - Revocation: Boolean flag
   - Storage: In-memory store (mock for development)

3. **Transaction Model**
   - Types: identity_create, credential_issue, transaction_submit
   - Status: pending, confirmed, failed

### Error Handling Strategy
1. **Network Restrictions**
   - Issue: Cannot pip install dependencies due to proxy (403 Forbidden)
   - Workaround: Skip testing in SRT sandbox, validate logic manually
   - Impact: Medium (tests run when environment allows)

2. **Git Workflow**
   - Issue: venv accidentally staged
   - Workaround: Reset venv, exclude in .gitignore
   - Push strategy: Push after each major feature, tag for review

### Integration Points
- **Frontend → Backend**
  - API: `gateway/app/routes.py` provides verification endpoints
  - Client: Next.js API client (lib/api.ts)
  - Authentication: Wallet-based routing (WalletRouter component)

- **Backend → Blockchain**
  - Solana: RPC client integration (future: feat-015)
  - Programs: Identity Core (feat-016), Credential Vault (feat-017)

- **Backend → Context Graph**
  - Storage: All verification decisions stored as traces
  - Query: Use before making similar decisions

## 🔄 Workflow Notes

### Current Workflow Pattern
```
1. Build Feature (Implementation skill)
   ├─ Query context-graph for precedents
   ├─ Write code (use DRAMS design)
   ├─ Write tests
   ├─ Type-check
   ├─ Run tests
   └─ Commit when ALL pass

2. Test Feature (Testing skill)
   ├─ Restart servers
   ├─ Run tests
   ├─ Browser testing
   └─ Return to tracker

3. Store Decision
   └─ mcp_context_store_trace (category, outcome, reasoning)
```

### Token Efficiency Strategy
- Use `mcp_execute_code` for >50 items (CSV processing, large log analysis)
- Batch context-graph queries when possible
- Compress at 70% / 85% when context gets large

### Known Issues
- [ ] feat-009 (Agent definitions) - Files created but commit status unclear
- [ ] MCP servers - Need to verify all tools are properly registered
- [ ] git status - Sometimes shows files as already tracked when they're not

### Next Immediate Actions
1. ✅ Complete feat-009 (Agent definitions) - Verify commit status
2. ⏳ Start feat-010 (Agent SDK integration)
3. ⏳ Build feat-011 (Orchestrator agent workflow)
4. ⏳ Create verification routes (feat-012)

### Context Graph Queries to Run
Before making architecture decisions:
- "FastAPI gateway backend choices"
- "MCP server architecture pattern"
- "Agent tool restrictions design"
- "Fraud detection risk scoring"

Before making fraud detection decisions:
- "Aadhaar document tampering patterns"
- "PAN card fraud precedents"
- "Document quality thresholds"

Before making compliance decisions:
- "Aadhaar Act 2019 data collection rules"
- "DPDP Act 2019 consent requirements"
- "Storage duration violation precedents"

---

**Last Updated:** 2026-02-03 16:25 UTC
**Agent:** Marvin (Clawdbot)

## 2026-03-17 Fraud And Compliance Contract Recovery

### Root Cause
- The gateway was reusing cached `ClaudeSDKClient` instances across concurrent background verification tasks.
- When Aadhaar/PAN submissions overlapped, the SDK transport hit `read() called while another coroutine is already waiting for incoming data`.
- That left verifications stuck in `fraud_check` or `parsing`, and the browser surfaced incomplete evidence contracts.

### Decision
- Create a fresh `ClaudeSDKClient` per agent invocation instead of caching clients on `AgentManager`.
- Apply the same timeout-plus-deterministic-fallback pattern used for document extraction to both fraud and compliance stages.
- Preserve provenance truthfully: fallback runs are marked `status=completed`, `model=deterministic-fallback`, and keep the original timeout/missing-contract error in provenance.

### Verification Evidence
- `python -m py_compile` passed for the gateway modules.
- `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest -q` passed with `11 passed`.
- API validation with readable synthetic Aadhaar and PAN uploads now completes the full pipeline instead of hanging.
- Live browser validation in the attached Chrome Beta session now shows:
  - `Document agent: completed`
  - `Fraud agent: completed`
  - `Compliance agent: completed`
  - `Evidence status: complete`

### Residual Gap
- The current document fallback extractor still misidentifies names on low-signal synthetic docs by preferring header text such as `UNIQUE IDENTIFICATION AUTHORITY OF INDIA` or `GOVT OF INDIA`.
- That is now an extraction-quality problem, not a contract/provenance problem. It should be handled in a separate follow-up branch.

## 2026-03-17 Trust Substrate Anchor

### Decision
- `aadhaar-chain` is the trust substrate, and downstream consumers must read a constrained trust surface rather than raw verification metadata.
- `VerificationStatus.metadata` remains the stable internal truth contract inside the repo.
- Public trust consumers must use `GET /api/identity/{wallet_address}/trust`, which exposes only:
  - workflow status and decision
  - evidence completeness
  - consent scope/purpose/reference
  - attestation summary/reference
  - revocation status
  - review status
  - audit receipt references

### Explicit Rejections
- Raw document bytes, OCR output, extracted PII, and full agent payloads must never become the downstream integration contract.
- Verification completion does not imply credential issuance.
- Manual review is a first-class governance state, not an implementation failure.

### Verification Evidence
- Backend route tests now confirm the trust surface redacts internal document/fraud/compliance evidence payloads.
- Live dashboard validation in the attached Chrome Beta session shows the first consumer reading trust artifacts from the trust surface:
  - PAN and Aadhaar appear as trust artifacts
  - evidence completeness is visible
  - consent is summarized without leaking raw verification metadata
  - audit references are visible

## 2026-03-17 Document Extraction Quality Recovery

### Root Cause
- The gateway document stage was still asking the document-validator agent to reason over a truncated base64 prefix instead of real OCR text.
- The gateway fallback extractor drifted away from the `document-processor` MCP server and only scraped printable bytes, which allowed image/PDF binaries to masquerade as text or produced low-signal field extraction.
- The gateway runtime was also stale: `gateway/venv` pointed at an old filesystem path, OCR packages were not installed in the active environment, and `tesseract` was missing from the machine.

### Decision
- Create a single shared document-processing implementation in `gateway/app/document_processing.py` and use it from both the gateway and `mcp-servers/document-processor/server.py`.
- Make the gateway document stage tool-backed by default: OCR/text extraction plus deterministic field parsing now forms the primary evidence contract, instead of the LLM parsing truncated base64.
- Treat OCR runtime failure as an explicit provenance failure and treat unreadable documents as explicit evidence gaps, rather than collapsing both cases into `missing_contract`.
- Repair the gateway execution environment so local verification matches the code:
  - rebuild `gateway/venv`
  - install `requirements.txt`
  - install `pytest`
  - install the `tesseract` system binary

### Verification Evidence
- `./venv/bin/python -m py_compile app/*.py tests/*.py ../mcp-servers/document-processor/server.py` passed.
- `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 ./venv/bin/pytest -q` passed with `13 passed`.
- Runtime API validation on live FastAPI routes with synthetic image uploads now returns tool-backed document provenance:
  - Aadhaar: extracted `name`, `dob`, and `uid` from PNG evidence with `model=document-processor-local`
  - PAN: extracted `name`, `dob`, and `pan_number` from PNG evidence with `model=document-processor-local`
- A matching Aadhaar submission and a matching PAN submission both reached `status=verified` with `evidence_status=complete` once the submitted claims matched the extracted document fields.

### Residual Gap
- Fraud and compliance are still falling back because the Claude SDK path is returning `Unknown message type: rate_limit_event` instead of a stable structured contract.
- Browser submission through the live Chrome session is still best driven over direct CDP attach; the built-in DevTools MCP browser remains isolated from the user's real wallet session.
