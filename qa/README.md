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
