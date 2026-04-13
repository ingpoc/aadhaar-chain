#!/bin/bash
if [ -f /tmp/gateway.pid ]; then
    kill $(cat /tmp/gateway.pid) 2>/dev/null
    rm /tmp/gateway.pid
fi
PORT="${PORT:-43101}"
lsof -ti:"$PORT" | xargs kill -9 2>/dev/null
echo "Gateway stopped"
