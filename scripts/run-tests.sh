#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/../gateway"
.venv/bin/python -m pytest tests/ -q "$@"
