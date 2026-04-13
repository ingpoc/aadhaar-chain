#!/bin/bash
set -euo pipefail

cd "$(dirname "$0")/../gateway"
PORT="${PORT:-43101}"

if command -v pyenv >/dev/null 2>&1; then
  PYTHON_BIN="$(pyenv which python3)"
else
  PYTHON_BIN="$(command -v python3)"
fi
PYTHONPATH_PREFIX=""
if [ -x "venv/bin/python3" ]; then
  PYTHON_BIN="venv/bin/python3"
elif [ -x "venv/bin/python" ]; then
  PYTHON_BIN="venv/bin/python"
else
  for candidate in venv/lib/python*/site-packages; do
    if [ -d "$candidate" ]; then
      PYTHONPATH_PREFIX="$PWD/$candidate"
      break
    fi
  done
fi

# Kill existing gateway on the configured port
lsof -ti:"$PORT" | xargs kill -9 2>/dev/null || true
# Start gateway in background
if [ -n "$PYTHONPATH_PREFIX" ]; then
  nohup env PORT="$PORT" PYTHONPATH="$PYTHONPATH_PREFIX${PYTHONPATH:+:$PYTHONPATH}" "$PYTHON_BIN" main.py > /tmp/gateway.log 2>&1 &
else
  nohup env PORT="$PORT" "$PYTHON_BIN" main.py > /tmp/gateway.log 2>&1 &
fi
echo $! > /tmp/gateway.pid
# Wait for health check
for i in {1..10}; do
    if curl -s "http://127.0.0.1:${PORT}/health" >/dev/null 2>&1; then
        echo "Gateway started: http://127.0.0.1:${PORT}"
        exit 0
    fi
    sleep 1
done
echo "Failed to start gateway"
exit 1
