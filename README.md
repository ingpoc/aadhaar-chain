# AadhaarChain

`aadhaar-chain` is the active trust producer for the portfolio and the platform behind **AgentGuard**. Product goal: [`GOAL.md`](GOAL.md). Workspace thesis: [`../PRODUCTIDEA.md`](../PRODUCTIDEA.md).

It owns identity anchors, verification state, consent references, review/revocation status, audit receipts, and downstream-safe trust reads — not raw Aadhaar/PAN/OCR on-chain or in consumer apps.

## Portfolio Role

- Produce the canonical trust state for a wallet.
- Keep raw identity evidence inside the trust producer boundary.
- Expose a narrow consumer contract for buyer, seller, FlatWatch, and agent-control-plane flows.
- Issue short-lived, audience-bound identity proof challenges that the connected wallet signs to prove control of a verified identity.

## Local Services

Expected local ports when started from the workspace:

| Service | URL |
| --- | --- |
| Frontend | `http://127.0.0.1:43100` |
| Gateway | `http://127.0.0.1:43101` |

Start from the workspace root when running the full portfolio:

```bash
scripts/portfolio/start-dev.sh
```

Start services directly:

```bash
cd gateway
PYTHONPATH="$PWD/venv/lib/python3.12/site-packages${PYTHONPATH:+:$PYTHONPATH}" PORT=43101 /Users/gurusharan/.pyenv/versions/3.12.0/bin/python3 main.py

cd ../frontend
npm run dev
```

## Trust API

Downstream apps should use the trust read surface, not raw identity evidence:

- `GET /api/identity/{wallet_address}`
- `GET /api/identity/{wallet_address}/trust`
- `GET /api/identity/status/{verification_id}`

Identity proof signing:

- `POST /api/identity/{wallet_address}/proof-token`
- `POST /api/identity/proof-token/verify`

The proof-token route issues a five-minute challenge only when the wallet trust state is `verified`. The verification route checks the exact message, wallet, audience, expiry, and wallet signature.

## Verification Evidence

Current verified checks:

```bash
cd gateway
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 /Users/gurusharan/.pyenv/versions/3.12.0/bin/python3 -m pytest tests/test_routes.py -q
```

The route tests cover trust reads, local trust fixtures, production storage guards, audit/review paths, and wallet-bound identity proof signing with a real Solana keypair.

Chrome validation in the signed wallet profile has produced:

- AadhaarChain dashboard transaction signing checkpoint.
- Buyer and seller identity proof signing through AadhaarChain proof challenges.

## Production Boundary

Production mode must not approve from fallback-only verification evidence and must not run on the local JSON trust store. Raw Aadhaar/PAN evidence remains inside the trust producer and is represented downstream only through safe summaries, references, and audit receipts.

See:

- `docs/architecture/trust-substrate-adr.md`
- `docs/architecture/aadhaar-pan-threat-model.md`
