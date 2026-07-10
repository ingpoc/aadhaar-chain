# Portfolio QA (control owner)

Sole owner of portfolio ledger, graders, and browser/wallet acceptance.

**Start here:** [`docs/workflow/README.md`](docs/workflow/README.md)

## Run

```bash
bash scripts/start-all.sh
npm run grade:deterministic
npm run grade:browser
npm run grade:wallet
```

| Port | Service |
| --- | --- |
| 43100 | AadhaarChain UI |
| 43101 | AadhaarChain gateway |
| 43102 | ONDC Buyer |
| 43103 | ONDC Seller |
| 43104 | FlatWatch API |
| 43105 | FlatWatch UI |

Ledger: `test-ledger.json`. Do not maintain copies in consumer repos.
