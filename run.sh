#!/usr/bin/env bash
# Start Lumix Remote: ./run.sh [--lan] [--camera-ip IP] [--port 8770] ...
set -euo pipefail
cd "$(dirname "$0")"
PY="${PYTHON:-python3}"
if [ ! -x .venv/bin/python ]; then
  echo "creating .venv ..."
  "$PY" -m venv .venv
fi
# Reinstall deps when requirements.txt changes.
if [ ! -f .venv/.req-stamp ] || ! cmp -s requirements.txt .venv/.req-stamp; then
  .venv/bin/python -m pip install -q --upgrade pip
  .venv/bin/python -m pip install -q -r requirements.txt
  cp requirements.txt .venv/.req-stamp
fi
export PYTHONUNBUFFERED=1
exec .venv/bin/python -m server "$@"
