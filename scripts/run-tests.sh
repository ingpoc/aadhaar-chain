#!/bin/bash
cd "$(dirname "$0")/../gateway"
source venv/bin/activate
if [ -d "tests" ]; then
    pytest tests/ -v
else
    echo "No tests directory found"
fi
