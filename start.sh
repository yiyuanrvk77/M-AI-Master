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
if ! python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)'; then
  echo "[error] Python 3.11 or later is required. Current: $(python3 --version 2>&1)"
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
OS_NAME=$(uname -s 2>/dev/null || echo unknown)
if [ "$OS_NAME" = "Darwin" ] && command -v ipconfig >/dev/null 2>&1; then
  for interface in en0 en1; do
    ip=$(ipconfig getifaddr "$interface" 2>/dev/null || true)
    [ -n "$ip" ] && echo "LAN:   http://${ip}:${MDM_PORT}"
  done
elif command -v hostname >/dev/null 2>&1; then
  for ip in $(hostname -I 2>/dev/null || true); do echo "LAN:   http://${ip}:${MDM_PORT}"; done
fi
echo "Health: http://127.0.0.1:${MDM_PORT}/api/health"

open_when_ready() {
  attempt=0
  while [ "$attempt" -lt 60 ]; do
    if command -v curl >/dev/null 2>&1 && curl -fsS "http://127.0.0.1:${MDM_PORT}/api/health" >/dev/null 2>&1; then
      if [ "$OS_NAME" = "Darwin" ] && command -v open >/dev/null 2>&1; then
        open "http://127.0.0.1:${MDM_PORT}/" >/dev/null 2>&1 || true
      elif command -v xdg-open >/dev/null 2>&1; then
        xdg-open "http://127.0.0.1:${MDM_PORT}/" >/dev/null 2>&1 || true
      fi
      return
    fi
    attempt=$((attempt + 1))
    sleep 1
  done
}
if [ "${MDM_NO_BROWSER:-0}" != "1" ]; then open_when_ready & fi

cd backend
exec ../.venv/bin/gunicorn --bind "${MDM_HOST}:${MDM_PORT}" --workers 1 --threads "${MDM_THREADS}" --timeout 120 --access-logfile - app:app
