# Aadhaar/PAN Evidence Threat Model

## Scope

This threat model covers private Aadhaar and PAN evidence handled by the `aadhaar-chain`
gateway while producing downstream-safe wallet trust state.

In scope:

- uploaded document bytes and file metadata
- document hashes and evidence references
- OCR output, extracted fields, and submitted claims
- consent, review, revocation, attestation, audit, and provenance records
- operator access to private evidence during manual review

Out of scope:

- downstream buyer, seller, or FlatWatch business data
- public chain program governance
- third-party KYC provider internal controls

## Assets

- Raw evidence: uploaded document bytes and any OCR or extracted PII.
- Evidence references: hashes, storage keys, content type, size, and verification IDs.
- Decision records: approval, rejection, manual review, revocation, and correction outcomes.
- Consent records: scope, purpose, status, wallet, verification ID, timestamp, and reference.
- Audit receipts: immutable records for creation, verification, consent, access, review, and revocation.
- Agent provenance: model, session, tool, status, and structured output references.

## Trust Boundaries

- Downstream consumers may read only `GET /api/identity/{wallet_address}/trust`.
- Raw evidence and extracted PII stay inside the gateway evidence boundary.
- Manual reviewers may access private evidence only through audited operator flows.
- PostgreSQL is the production trust store for identity, verification, consent, review,
  decision, revocation, attestation, audit, and provenance records.
- Local JSON state is only for demo and fixture use.

## Threats And Required Controls

| Threat | Required control |
| --- | --- |
| Raw Aadhaar/PAN data leaks through downstream APIs | Trust responses must redact raw document bytes, Aadhaar/PAN numbers, OCR text, extracted fields, submitted claims, and full agent reasoning. |
| Evidence is approved without valid consent | Aadhaar verification records must include consent scope, purpose, status, timestamp, wallet address, verification ID, and consent reference. Missing consent must block high-trust approval. |
| Evidence is changed after review | Store a content hash before processing and preserve the evidence reference used by the decision record. |
| Operators access evidence without traceability | Every evidence access must append an audit receipt with reviewer/operator identity, purpose, wallet, verification ID, and timestamp. |
| Local files become production trust storage | Production startup must require `TRUST_STORE_BACKEND=postgres` and `DATABASE_URL`. |
| Stale approvals survive revocation | Revocation records must be separate from verification records and must force downstream trust to `revoked_or_blocked`. |
| Review decisions are not reconstructable | Review, decision, consent, audit, and provenance records must be stored independently enough to reconstruct why trust changed. |
| Keys are exposed or cannot rotate | Evidence encryption keys must come from environment-managed key material or a KMS. Key identifiers must be stored with evidence references, and rotation must rewrap stored evidence without changing trust references. |
| Private evidence remains longer than needed | Retention policy must define evidence expiration by verification state. Deletion must remove raw evidence while preserving downstream-safe audit receipts and decision references. |
| Breach detection depends on application logs | Evidence access, failed access, review decisions, revocations, and anomalous read volume must emit structured audit events suitable for monitoring. |

## Retention And Deletion Policy

- Rejected or revoked evidence: retain only while appeal/correction windows are active, then delete raw evidence and keep audit/decision references.
- Verified evidence: retain raw evidence only for the minimum compliance period required by the operating policy; preserve hashes and references after deletion.
- Manual review evidence: retain until the review, appeal, or correction workflow is closed.
- Deletion must be auditable and must not remove the fact that a decision occurred.

## Encryption And Key Management

- Raw evidence storage must be encrypted at rest.
- Encryption keys must not be committed or stored in local fixture files.
- Production key material must be supplied by environment or KMS-backed configuration.
- Evidence references should include key version or key ID so old evidence can be rewrapped during rotation.
- Rotation must be tested by decrypting with the old key, re-encrypting with the new key, and preserving the same trust decision references.

## Access Logging And Breach Monitoring

Minimum audit events:

- `identity_created`
- `verification_requested`
- `consent_recorded` or `consent_missing`
- `verification_decision`
- `evidence_accessed`
- `review_decision_recorded`
- `trust_revoked`
- downstream trust reads

Minimum monitoring signals:

- evidence access outside expected reviewer sessions
- repeated failed evidence access
- trust reads for revoked or blocked wallets
- production startup without PostgreSQL storage
- verification approval with missing consent or fallback-only evidence

## Current Implementation Mapping

- `gateway/app/state_store.py` owns local-file and PostgreSQL trust persistence.
- `gateway/app/routes.py` owns downstream trust reads, fixture seeding, review decisions,
  evidence-access audit receipts, and revocation handling.
- `gateway/app/agent_manager.py` owns verification workflow decisions and provenance.
- `gateway/tests/test_routes.py` covers redaction, persistence guards, review audit flows,
  and revocation trust-state transitions.

## Open Production Hardening

- Connect PostgreSQL schema management to the deployment pipeline instead of relying only on gateway startup schema creation.
- Replace local encrypted evidence files with encrypted object storage when the deployment target provides a managed private bucket.
- Add KMS-backed key rotation tests for deployed evidence storage.
- Add breach-monitoring alerts from the audit receipt stream.
