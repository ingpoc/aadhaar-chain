# Session Friction Log → Standing Rules

Captured from the portfolio QA hardening session. Keep this short; promote durable rules into the two workflow docs above.

| Friction | Standing rule |
| --- | --- |
| `mkdir /agent/...` permission denied aborted `git checkout -b` via `&&` | Create branches with separate commands; write under repo paths or `$HOME` |
| Python `.venv` failed without ensurepip | Install `python3.12-venv` before `python3 -m venv` |
| `@portfolio/trust-client` missing at `../shared` | Vendor under each app `shared/trust-client` and alias `./shared/...` |
| No browser MCP in cloud agent | Playwright in `qa/browser` is the browser control plane |
| AGENTS.md pointed at missing `../docs/workflow/*` | Use `qa/docs/workflow/*` in aadhaar-chain; update local AGENTS pointers |
| Phantom connect flaky | Real pubkey `toBytes()` + click Phantom text; wait for truncated pubkey button |
| FlatWatch Sign in flake | Wait for Sign in **or** authenticated content after verifying-session |
| Checkout Get quote disabled | Fill billing by label; require saved `buyer.name` + `contact.email` |
| Receipts `Invalid time value` | Parse ISO `created_at`; never assume unix seconds |
| Trust UI vs API mismatch | Elevated FlatWatch writes must call `require_verified_wallet_trust` |
| Buyer order invisible to seller | Shared SKUs + `ondc-portfolio-demo-orders` bridge |

When you hit a new friction: add one row here, then encode the rule into the control-plane doc if it will recur.
