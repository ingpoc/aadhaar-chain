# AadhaarChain Decomposition Goal

## Status

**Legacy source checkout, not an active product.** The current gateway hosts
working AgentGuard code, but AadhaarChain identity and blockchain functionality
are not part of the product roadmap. Buyer and Seller are the active relying
applications.

## Goal

Extract the smallest reusable AgentGuard control plane from this checkout while
preserving Seller and completing Buyer AgentGuard demonstrations. Do not expand
AadhaarChain as an identity platform.

## Move into AgentGuard ownership

- agent registration and active/paused status;
- deterministic mandate storage and evaluation;
- Buyer checkout and Seller refund-limit enforcement;
- one-time exception approval and replay protection;
- signed Intent Receipts and verification;
- minimal authenticated-principal adapter;
- tests for allow, escalation, approval, replay, pause, and receipt privacy.

## Keep only as replaceable adapters

- local Buyer and Seller principal fixtures for the demonstration;
- external identity, business, or organization-role assurance only when a real
  customer requirement demands it.

## Retire from the active product

- standalone AadhaarChain frontend and identity dashboard;
- wallet-first identity onboarding;
- Aadhaar/PAN verification as a product-owned workflow;
- portfolio SSO as a product capability;
- connected-app directory and reusable identity-wallet positioning;
- Solana bridge, on-chain identity, credentials, reputation, and staking;
- land, credit, scoring, SSI, and “trusted everywhere” concepts;
- any claim of NPCI, UPI, UIDAI, bank, or production ONDC integration.

## Migration invariants

- Preserve the working Seller AgentGuard lane and Buyer checkout policy tests
  throughout decomposition.
- Do not delete identity or SSO code until all live imports, routes, scripts, and
  regression contracts are inventoried.
- Protected actions remain server-enforced and fail closed.
- Approval remains exact, short-lived, persisted, and consumed once.
- Pause is checked before every action.
- Raw identity, prompts, payees, orders, and commercial evidence do not enter
  AgentGuard operational state.
- Existing dirty user changes are preserved and migrated deliberately.

## Completion criteria

1. AgentGuard has an explicit service/module owner independent of the AadhaarChain
   product name.
2. ONDC Buyer and Seller complete allow, exception, approval, replay, pause, and
   receipt verification through that owner.
3. Buyer and Seller authentication supply principals without AadhaarChain
   onboarding.
4. AadhaarChain-only routes and UI are unreachable from the active demo.
5. Buyer and FlatWatch regression boundaries remain explicit.
6. Documentation and scripts no longer present AadhaarChain as an active product.

## Source of truth

This file owns decomposition of the legacy `aadharchain/` checkout.
`../PRODUCTIDEA.md` owns the AgentGuard product thesis,
`../IMPLEMENTATIONPLAN.md` / `../TESTINGPLAN.md` own demo build and proof,
and `../ondcbuyer/GOAL.md` + `../ondcseller/GOAL.md` own active app outcomes.
