#!/usr/bin/env sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$SCRIPT_DIR"

echo "============================================"
echo "  M-AI Master - Flask startup"
echo "============================================"

if ! command -v python3 >/dev/null 2>&1; then
    echo "[error] Python 3.11 or later is required."
    exit 1
fi

echo "[1/2] Checking dependencies..."
python3 -m pip install -r backend/requirements.txt -q

echo "[2/2] Starting Flask..."
echo "Home: http://127.0.0.1:5000"
echo "Health: http://127.0.0.1:5000/api/health"

cd backend
exec python3 app.py
