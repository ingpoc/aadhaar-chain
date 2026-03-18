# Aadhaar Chain

`aadhaar-chain` is the trust substrate for the workspace.

It is responsible for:

- wallet-bound identity anchors
- verification workflows with explicit provenance
- consent-aware trust state
- downstream-safe trust artifacts

It is explicitly **not** a system for putting raw identity data on-chain.

## Current Safe Read Surface

- `GET /api/identity/{wallet_address}`
- `GET /api/identity/{wallet_address}/trust`
- `GET /api/identity/status/{verification_id}`

Use `/trust` for downstream consumption. It exposes trust decisions, evidence completeness, consent references, review state, and audit references without leaking raw verification evidence.

## Architecture

The governing trust-boundary decision is documented in [docs/architecture/trust-substrate-adr.md](docs/architecture/trust-substrate-adr.md).
