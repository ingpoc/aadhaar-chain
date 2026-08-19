#!/usr/bin/env bash
# Idempotent repository bootstrap for Cloud Agents.
# Refreshes gateway (Python) and frontend (Node) dependencies after checkout.
# System packages (python3.12-venv, tesseract-ocr) come from the base snapshot.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "=== Gateway: Python dependencies ==="
cd "$REPO_ROOT/gateway"
if [ ! -x ".venv/bin/python" ]; then
  python3 -m venv .venv
fi
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt

echo "=== Frontend: Node dependencies ==="
cd "$REPO_ROOT/frontend"
npm install

echo "=== Install complete ==="
