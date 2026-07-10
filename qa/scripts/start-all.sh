#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LOGS="$ROOT/artifacts/logs"
mkdir -p "$LOGS"

start_one() {
  local name="$1"
  local cwd="$2"
  local cmd="$3"
  local health="$4"
  echo "[start] $name"
  (
    cd "$cwd"
    nohup bash -lc "$cmd" >"$LOGS/$name.log" 2>&1 &
    echo $! >"$LOGS/$name.pid"
  )
  for i in $(seq 1 60); do
    if curl -sf "$health" >/dev/null 2>&1; then
      echo "[ready] $name"
      return 0
    fi
    sleep 1
  done
  echo "[warn] $name not healthy yet; see $LOGS/$name.log"
  tail -n 40 "$LOGS/$name.log" || true
}

# AadhaarChain gateway
start_one "ac-gateway" \
  "/agent/repos/aadhaar-chain/gateway" \
  "PORT=43101 HOST=127.0.0.1 .venv/bin/python main.py" \
  "http://127.0.0.1:43101/health"

# AadhaarChain frontend
start_one "ac-frontend" \
  "/agent/repos/aadhaar-chain/frontend" \
  "npm run dev" \
  "http://127.0.0.1:43100"

# FlatWatch backend
start_one "fw-backend" \
  "/agent/repos/flatwatch/backend" \
  ".venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 43104" \
  "http://127.0.0.1:43104/api/health"

# FlatWatch frontend
start_one "fw-frontend" \
  "/agent/repos/flatwatch/frontend" \
  "npm run dev" \
  "http://127.0.0.1:43105"

# Buyer
start_one "ondc-buyer" \
  "/agent/repos/ondc-buyer" \
  "npm run dev" \
  "http://127.0.0.1:43102"

# Seller
start_one "ondc-seller" \
  "/agent/repos/ondc-seller" \
  "npm run dev" \
  "http://127.0.0.1:43103"

echo "All start attempts complete"
