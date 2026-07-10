# Session Friction Log → Standing Rules

Append-only. Promote durable rules into `browser-testing-control-plane.md`.  
Not a second control owner.

| Friction | Standing rule |
| --- | --- |
| `mkdir /agent/...` permission denied aborted `git checkout -b` via `&&` | Create branches with separate commands; write under repo paths or `$HOME` |
| Python `.venv` failed without ensurepip | Install `python3.12-venv` before `python3 -m venv` |
| `@portfolio/trust-client` missing at `../shared` | Vendor under each app `./shared/trust-client`; Vite/Vitest/tsconfig aliases must use `./shared/...` |
| No browser MCP in cloud agent | Playwright in `aadhaar-chain/qa/browser` is the browser control plane |
| AGENTS pointed at missing `../AGENTS.md` or `../docs/workflow/*` | Sole owner is `aadhaar-chain/qa/docs/workflow/`; repo AGENTS are scope + pointer only |
| Duplicate `qa/test-ledger.json` in consumer repos | Delete forks; ledger of record lives only in `aadhaar-chain/qa` |
| Seller docs/scripts still said port 3002 | Seller is **43103** everywhere |
| Phantom connect flaky | Real pubkey `toBytes()` + click Phantom text; wait for truncated pubkey button |
| FlatWatch Sign in flake | Wait for Sign in **or** authenticated content after verifying-session |
| Checkout Get quote disabled | Fill billing by label; require saved `buyer.name` + `contact.email` |
| Receipts `Invalid time value` | Parse ISO `created_at`; never assume unix seconds |
| Trust UI vs API mismatch | Elevated FlatWatch writes must call `require_verified_wallet_trust` |
| Buyer order invisible to seller | Shared SKUs + `ondc-portfolio-demo-orders` bridge |

When you hit a new friction: add one row here, then encode the rule into the control-plane doc if it will recur.
