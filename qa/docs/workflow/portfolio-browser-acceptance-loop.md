# Portfolio Browser Acceptance Loop

Same-user, same-wallet journey across the portfolio.  
Prerequisite: ownership entry `README.md`, then `browser-testing-control-plane.md`.

## Journey order (fixed)

1. **AadhaarChain** (trust producer) — connect wallet; identity present; trust `verified` (fixture or real verify).
2. **ONDC Buyer** — discovery → cart → trust-gated checkout; `Sign buyer proof` → `Identity signed`.
3. **ONDC Seller** — dashboard trust; `Sign seller proof` → `Identity signed`; accept/dispatch order.
4. **FlatWatch** — app-local Sign in; connect same wallet; elevated receipt/challenge CTAs unlock only when trust is verified.

Do not start at buyer/seller/FlatWatch and blame those apps for missing trust state.

## One-command acceptance

```bash
cd /agent/repos/aadhaar-chain/qa
bash scripts/start-all.sh
npm run grade:deterministic
npm run grade:browser
npm run grade:wallet
```

`grade:wallet` is the real same-wallet acceptance. Unsigned browser smoke alone is insufficient.

## Per-app success criteria (minimum)

### AadhaarChain
- Gateway healthy
- Injected wallet connects (truncated pubkey on button)
- Trust read for that wallet is `verified` after fixture/verify
- Trust surface has no raw Aadhaar/PAN fields

### Buyer
- Categories + search results actionable
- With verified wallet: trust chip verified; **Identity signed** after proof button
- Checkout: billing saved → Get quote → Place order → `/orders/:id`
- Without verified trust: elevated CTA explicitly blocked (not vague disable)

### Seller
- Catalog lists shared demo SKUs with buyer
- With verified wallet: **Identity signed**
- Orders queue shows Accept/Reject/Dispatch; can accept pending/bridged order

### FlatWatch
- Sign in completes (wait out verifying-session race)
- Sync/summary works with JWT
- Without wallet/trust: Upload/Submit show **Trust required**
- With verified wallet: Upload receipt enabled; server rejects elevated writes without verified trust

## Debug sequence when a step fails

1. Confirm ports/health (control plane checklist).
2. Classify failure (trust vs commerce vs demo bridge vs wallet adapter).
3. Re-seed fixture: `POST /api/identity/dev/fixtures/{wallet}` with `fixture_state=verified`.
4. Re-run only the failing grader/spec — do not restart the whole matrix until classified.
5. Fix root owner repo; re-run deterministic then browser/wallet for that flow id.

## Anti-patterns from prior sessions

- Treating missing shared `trust-client` as a buyer business-logic bug
- UI-only trust gates without server `X-Wallet-Address` enforcement
- Buyer/seller demo SKUs that do not match
- Assuming Phantom modal entries are always `role=button`
- Injecting wallet with empty `toBytes()` (adapter builds wrong PublicKey)
- Committing local `.env` / keypairs
- Declaring portfolio green from unit tests alone

## Exit criteria

Portfolio acceptance is green only when:

- ledger flows for the journey have deterministic pass where applicable
- `portfolio.spec.ts` pass
- `wallet-journey.spec.ts` pass on one generated keypair across all four apps
