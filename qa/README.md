# Portfolio QA Ledger

Use-case-backed test ledger + deterministic graders + Playwright browser control.

## Services

| App | URL |
| --- | --- |
| AadhaarChain UI | http://127.0.0.1:43100 |
| AadhaarChain gateway | http://127.0.0.1:43101 |
| ONDC Buyer | http://127.0.0.1:43102 |
| ONDC Seller | http://127.0.0.1:43103 |
| FlatWatch API | http://127.0.0.1:43104 |
| FlatWatch UI | http://127.0.0.1:43105 |

## Run

```bash
# from portfolio-qa harness (or this qa/ folder after npm install)
npm run start:all
npm run grade:deterministic
npm run grade:browser
```

Ledger: `test-ledger.json` — every flow maps to a product use case and concrete success criteria.

## Same-wallet signed journey

```bash
npm run grade:wallet
```

Creates a Solana keypair, injects a Phantom-compatible provider into Playwright,
seeds verified AadhaarChain trust, then exercises:

1. AadhaarChain wallet connect
2. Buyer `Sign buyer proof` → `Identity signed`
3. Buyer checkout → demo order
4. Seller `Sign seller proof` → `Identity signed`
5. Seller order accept
6. FlatWatch verified-wallet elevated receipt CTA
