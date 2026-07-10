# Portfolio Browser Testing Control Plane

**Sole owner:** `aadhaar-chain/qa` (see `README.md` in this folder).  
Trust producer is the first checkpoint. Use this before any portfolio browser or wallet-signed debugging.

Do not maintain a second ledger, grader tree, or workflow doc in FlatWatch / Buyer / Seller.

## First command

```bash
cd /agent/repos/aadhaar-chain/qa
bash scripts/start-all.sh
npm run grade:deterministic
npm run grade:browser
npm run grade:wallet
```

If `/agent/repos` is not writable, copy or symlink **this entire `qa/` tree** elsewhere — do not fork workflow docs or the ledger.

Ledger of record: `aadhaar-chain/qa/test-ledger.json` only.

## Loop (do not skip)

1. **Use-case first** — name what each app must achieve for a real user (not “page loads”).
2. **Ledger** — every action gets: id, route/API, expected label/outcome, success criteria, grader type.
3. **Deterministic first** — API/policy/unit graders. Cheap, reproducible.
4. **Browser control second** — Playwright against live local ports. Real UI friction lives here.
5. **Same-wallet third** — inject Phantom-compatible wallet + real ed25519 signing.
6. **Fix root cause** — trust vs commerce vs demo-isolation vs adapter contract. Do not patch UI-only.

## Service map

| Port | Service |
| --- | --- |
| 43100 | AadhaarChain frontend |
| 43101 | AadhaarChain gateway (trust API) |
| 43102 | ONDC Buyer |
| 43103 | ONDC Seller |
| 43104 | FlatWatch backend |
| 43105 | FlatWatch frontend |

Health before product conclusions:

- `GET :43101/health`
- `GET :43104/api/health`
- frontends return HTTP &lt; 500 on critical routes

## Failure taxonomy (do not collapse)

| Symptom class | Likely owner | Wrong conclusion |
| --- | --- | --- |
| Trust chip `no_identity` / proof 403 | AadhaarChain gateway / fixture / wallet pubkey | “Buyer is broken” |
| Checkout disabled: billing validation | Buyer form/session save | “Trust gate failed” |
| Catalog empty / SKU miss | Demo catalog alignment | “Search API down” |
| Seller missing buyer order | Demo order bridge key | “Fulfillment bug” |
| FlatWatch `Trust required` with JWT | Missing `X-Wallet-Address` or unverified trust | “Auth broken” |
| `Invalid time value` on receipts | ISO `created_at` parsed as unix seconds | “Upload failed” |
| Wallet stays `Select Wallet` | Injected provider `toBytes()` / modal click | “Phantom not installed” |

## Environment friction checklist (this workspace)

Run these before inventing new setup paths:

1. **Writable paths** — `/agent` and `/agent/repos` may be root-owned. Write under `/agent/repos/<repo>/…` or `/home/ubuntu/…`. Do not `mkdir /agent/foo && git checkout` in one `&&` chain (mkdir failure aborts branch creation).
2. **Python venv** — need `python3.12-venv` / ensurepip. Recreate broken `.venv` after installing the package.
3. **Missing `@portfolio/trust-client`** — buyer/seller Vite aliases must point at vendored `./shared/trust-client/src/index.ts` (not missing `../shared/...`).
4. **No browser MCP** — use Playwright Chromium in `qa/browser`.
5. **Local env** — buyer/seller need `VITE_COMMERCE_DEMO_MODE=true` and trust URLs on `127.0.0.1:43101`. Do not commit secrets; gitignore `.env`.
6. **Ignore local runtime DB/uploads** when concluding product bugs; prefer fresh fixture wallet.

## Wallet injection contract (required)

Phantom adapter does:

```js
publicKey = new PublicKey(wallet.publicKey.toBytes())
```

So the injected provider **must**:

- set `window.solana.isPhantom` and `window.phantom.solana`
- return **real 32-byte pubkey** from `toBytes()` (not zeros)
- implement `connect`, `signMessage`, `on`/`off`
- sign via Node/solders (`exposeFunction`) for real ed25519

Connect UI: open **Select Wallet**, click text **Phantom** (not only `role=button` named Phantom). Success = button shows truncated pubkey like `EL4m..6fFu`.

Implementation: `qa/browser/wallet.ts` + `qa/browser/wallet-journey.spec.ts`.

## FlatWatch auth race

`ProtectedRoute` shows **Verifying session** before **Sign in**. Do not treat missing Sign-in as already authenticated. Wait for either Sign in or authenticated content, then click Sign in and wait for `/api/auth/login`.

## Buyer checkout selectors

Billing inputs use React `useId()` prefixes. Fill by **label** (`Full name`, `Email`, `Phone`), then Save / blur. `Get quote` stays disabled until `session.buyer.name` and `session.buyer.contact.email` exist.

## Demo commerce interop

- Shared SKUs: `basmati-rice-5kg`, `mustard-oil-1l`
- Buyer checkout publishes to `localStorage['ondc-portfolio-demo-orders']`
- Seller order list merges that bridge key

If these diverge again, portfolio E2E is narrative-only.

## Evidence standard

Before claiming a fix:

1. Deterministic grader green for the owning flow id in `test-ledger.json`
2. Browser or wallet journey assertion green for the same id
3. Name the root cause class from the taxonomy above

## Related

- Ownership / entry map: `README.md`
- Same-user acceptance order: `portfolio-browser-acceptance-loop.md`
- Friction log: `session-friction-log.md`
- Ledger: `../../test-ledger.json`
