#!/usr/bin/env bash
# Idempotent repository bootstrap for Cloud Agents.
# Installs the system packages and refreshes the gateway (Python) and
# frontend (Node) dependencies after checkout. Safe to run repeatedly.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "=== System packages ==="
# python3-venv: create the gateway virtualenv.
# tesseract-ocr: OCR binary required by pytesseract in the document-validator.
need_apt=""
command -v tesseract >/dev/null 2>&1 || need_apt="yes"
python3 -c "import ensurepip" >/dev/null 2>&1 || need_apt="yes"
if [ -n "$need_apt" ]; then
  sudo apt-get update -qq
  sudo apt-get install -y -qq python3-venv tesseract-ocr
else
  echo "System packages already present, skipping apt."
fi

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
