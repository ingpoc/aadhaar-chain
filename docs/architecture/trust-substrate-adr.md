# ADR: Aadhaar Chain Trust Substrate Boundary

## Status

Accepted on 2026-03-17.

## Decision

`aadhaar-chain` is the trust substrate for the workspace, not a place to publish raw identity data.

The system boundary is:

- raw document bytes, OCR output, extracted PII, and agent-internal reasoning remain backend-only evidence
- on-chain or downstream-safe artifacts are limited to identity anchors, commitments, verification state, consent references, review status, attestation references, revocation markers, and audit receipt references
- downstream apps consume the trust-read contract, never the internal verification payloads

## Trust Artifacts

The substrate may expose only these trust artifacts to downstream consumers:

- verification decision and workflow status
- evidence completeness state
- consent scope, purpose, and reference
- attestation or credential summary/reference
- revocation status
- review status and review reference
- audit receipt references

Current public read surface:

- `GET /api/identity/{wallet_address}/trust`

This surface is versioned as `v1` and intentionally excludes raw extracted fields, document hashes, OCR output, and full agent structured outputs.

## Consent Model

- Aadhaar processing requires explicit consent in the backend request
- consent is scoped to `identity_verification`
- consent should be represented downstream as a reference plus scope/purpose summary, not as raw form payloads
- PAN currently does not require the same explicit consent contract and is marked `not_required`

## Verification And Approval Policy

- default posture is strict manual review until real tool-backed document, fraud, and compliance outputs are reliable
- fallback contracts may keep the workflow legible and auditable, but they do not justify relaxed approval policy
- no stage may be treated as trustworthy unless provenance is explicit
- `VerificationStatus.metadata` is the stable internal verification truth contract inside `aadhaar-chain`
- downstream consumers must not depend on `metadata.document.extracted_fields`, `metadata.document.submitted_claims`, or any other internal evidence payload

## Credential, Review, And Revocation Model

- verification completion does not automatically mean credential issuance
- attestation/credential references remain separate trust artifacts and may stay `not_issued` even when verification evidence is complete
- manual review is a first-class trust state, not an error case
- revocation applies to issued downstream trust artifacts, not to raw document evidence

## Audit Model

- every verification workflow must have a stable verification record reference
- final decisions should have a separate decision record reference
- consented workflows should expose a consent record reference when consent exists
- audit references are safe for downstream consumption; the underlying raw evidence is not

## Rejected Patterns

- storing raw Aadhaar, PAN, OCR output, or extracted PII on a public chain
- auto-approving verification because a fallback heuristic found plausible fields
- exposing internal document/fraud/compliance payloads as a downstream integration contract
- allowing downstream apps to infer trust from frontend-only state or local timers
- treating `manual_review` as a failure to model the system rather than an explicit governance state

## Consequences

- `aadhaar-chain` must continue hardening toward real tool-backed extraction, fraud, and compliance outputs
- downstream apps must integrate through trust-read adapters, not direct verification internals
- human review outcome recording and durable attestation issuance are next substrate workstreams
