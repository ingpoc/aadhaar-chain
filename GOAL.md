# AadhaarChain Goal

## Product

AadhaarChain is a privacy-preserving trust and authorization platform for
verified people, organizations, and the AI agents acting for them.

It converts an approved identity or business-verification result into minimal,
purpose-bound, revocable digital assurance. Applications can verify authority
without receiving Aadhaar, PAN, biometrics, OCR output, or other raw evidence.

## Product promise

> Verify once. Disclose minimally. Delegate safely. Revoke anytime.

## Primary customers

- Applications that need assurance for consequential actions but should not
  custody identity evidence.
- Businesses that want AI agents to act within explicit, auditable limits.
- People who want reusable verification, clear consent, and control over access.

ONDC Seller is the first relying product. ONDC Buyer and FlatWatch are the next
consumer applications.

## Customer jobs

1. Establish that a consenting principal meets a required assurance level.
2. Share only the claim needed by a particular application and purpose.
3. Prove that the principal controls the approving cryptographic account.
4. Delegate bounded authority to a person, service, or AI agent.
5. Suspend or revoke trust and delegated authority across relying parties.
6. Produce independently verifiable approval and action receipts.
7. Recover safely after device or key loss.

## Owned capabilities

- lawful identity and business-assurance provider integrations;
- private evidence, consent, review, retention, and deletion lifecycle;
- minimal, audience-specific trust claims;
- principal, issuer, relying-party, and AI-agent registries;
- deterministic delegation and action policies;
- one-time, action-bound proof issuance and verification;
- suspension, revocation, key rotation, and threshold recovery;
- PII-free shared commitments and audit receipts;
- relying-party SDKs and operational governance.

AgentGuard is the flagship product experience delivered by these capabilities.
It lets verified principals authorize AI agents for precise actions and limits.

## Trust boundary

### Private and off-chain

- Aadhaar, PAN, biometrics, documents, OCR, extracted PII, prompts, and private
  evidence;
- detailed policies, business records, and AI reasoning;
- provider credentials and investigation data.

### Shared or on-chain

- opaque issuer, principal, agent, and policy references;
- minimal status, assurance, expiry, suspension, and revocation state;
- domain-separated commitments to approvals and receipts;
- governed program and upgrade-authority evidence.

## Hard rules

- Aadhaar data and deterministic Aadhaar-derived hashes never go on-chain.
- Relying parties receive only purpose-required claims.
- Stable public identifiers must not enable cross-application tracking.
- Proofs bind action, resource, amount, audience, origin, nonce, and expiry.
- Successful proofs are atomically consumed once and re-check live revocation.
- AI may propose a policy; deterministic code enforces it.
- Unknown, stale, or contradictory trust fails closed for consequential actions.
- Passkeys and sponsored transactions hide blockchain complexity from users.
- No transferable identity credentials or universal reputation score.
- UIDAI or another competent issuer establishes identity; AadhaarChain does not
  infer identity with an LLM or imply government endorsement.

## Phase-one outcome

Deliver one end-to-end ONDC Seller AgentGuard journey:

1. Establish seller assurance.
2. Register a seller operations agent.
3. Create a structured policy from plain language.
4. Permit an in-limit action.
5. Block an over-limit refund and request human approval.
6. Consume the approval once and reject replay.
7. Pause the agent and reject subsequent action.
8. Verify the receipt without exposing identity evidence.
9. Recover the principal account safely.

## Success measures

- At least two organizationally independent applications verify the same trust
  or delegation contract.
- At least 80% of pilot users create a policy without support.
- At least 90% correctly understand action, limit, recipient, and consequence.
- Zero successful proof replays or unauthorized protected actions.
- Revocation reaches pilot consumers within 30 seconds.
- All audit receipts are independently reproducible.
- Customers demonstrate reduced identity custody or approval workload.

## Non-goals

- Rebuilding UIDAI authentication, liveness, or biometric deduplication.
- Putting identity documents or government identifiers on-chain.
- Replacing authoritative land, banking, credit, or government registries.
- Tokenizing legal title without an enabling legal and registry framework.
- Launching a speculative token, identity NFT, or universal trust score.
- Treating a wallet connection or SSO cookie as elevated authorization.

## Source of truth

This file owns the AadhaarChain product goal. Workspace integration and current
runtime status remain in the root `AGENTS.md` and `PRODUCTION-READINESS.md`.
Architecture boundaries remain in `docs/architecture/`.
