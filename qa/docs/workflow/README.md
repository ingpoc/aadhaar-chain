# Portfolio control plane — sole owner

**Control owner:** `aadhaar-chain/qa`  
**Authority path:** this directory (`qa/docs/workflow/`)

There is no parent workspace `../AGENTS.md` in the multi-repo checkout. Do not invent one. Do not create a second portfolio workflow tree under FlatWatch, ONDC Buyer, or ONDC Seller.

## Entry map (read in order)

| Order | Doc | Role |
| --- | --- | --- |
| 1 | `browser-testing-control-plane.md` | First command, service map, failure taxonomy, env/wallet contracts |
| 2 | `portfolio-browser-acceptance-loop.md` | Same-wallet journey order + exit criteria |
| 3 | `session-friction-log.md` | Append-only friction → standing rules (promote durable rules into #1) |

## Owned artifacts (do not fork)

| Artifact | Path |
| --- | --- |
| Ledger of record | `aadhaar-chain/qa/test-ledger.json` |
| Deterministic graders | `aadhaar-chain/qa/graders/` |
| Playwright browser control | `aadhaar-chain/qa/browser/` |
| Start-all / helpers | `aadhaar-chain/qa/scripts/` |

Consumer repos may keep a thin `qa/README.md` pointer only. No ledger copy. No grader copy. No parallel workflow docs.

## Repo-local AGENTS.md contract

Each app `AGENTS.md` may declare only:

1. Repo scope / commands / boundaries
2. A pointer to this control owner for portfolio browser/wallet work
3. Repo-specific verification that does not contradict the control plane

If a local AGENTS.md conflicts with this control plane on portfolio ports, ledger, wallet contract, or journey order — **this directory wins**.

## First command

```bash
cd /agent/repos/aadhaar-chain/qa
bash scripts/start-all.sh
npm run grade:deterministic
npm run grade:browser
npm run grade:wallet
```

If `/agent/repos` is not writable, copy or symlink **this entire `qa/` tree** elsewhere — do not fork workflow docs or the ledger.
