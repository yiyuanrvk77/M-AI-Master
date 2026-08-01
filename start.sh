#!/usr/bin/env sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$SCRIPT_DIR"

if [ -f backend/.env ]; then
  set -a
  . backend/.env
  set +a
fi

: "${MDM_HOST:=0.0.0.0}"
: "${MDM_PORT:=5000}"
: "${MDM_THREADS:=8}"
: "${MDM_PRODUCTION:=1}"
: "${MDM_DB_PATH:=../runtime/data/mdm_data.db}"
: "${MDM_LOG_DIR:=../runtime/logs}"
export MDM_HOST MDM_PORT MDM_THREADS MDM_PRODUCTION MDM_DB_PATH MDM_LOG_DIR

if ! command -v python3 >/dev/null 2>&1; then
  echo "[error] Python 3.11 or later is required."
  exit 1
fi

mkdir -p runtime/data runtime/logs
if [ ! -x .venv/bin/python ]; then
  echo "[1/3] Creating isolated Python environment..."
  python3 -m venv .venv
fi

echo "[2/3] Installing/checking dependencies..."
.venv/bin/python -m pip install -r backend/requirements.txt -q

echo "[3/3] Starting production WSGI server..."
echo "Local: http://127.0.0.1:${MDM_PORT}"
if command -v hostname >/dev/null 2>&1; then
  for ip in $(hostname -I 2>/dev/null || true); do echo "LAN:   http://${ip}:${MDM_PORT}"; done
fi
echo "Health: http://127.0.0.1:${MDM_PORT}/api/health"
cd backend
exec ../.venv/bin/gunicorn --bind "${MDM_HOST}:${MDM_PORT}" --workers 1 --threads "${MDM_THREADS}" --timeout 120 --access-logfile - app:app
