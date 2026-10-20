#!/usr/bin/env bash
# Verify host + container requirement files resolve on a clean venv (dry-run).
# Slower than bootstrap but catches pin conflicts before teammates hit them.
# Usage: bash scripts/verify_deps.sh
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${PYTHON:-python3.12}"
HOST_REQ="$REPO_ROOT/authorization_eva_sandbox/requirements-host.txt"
CONTAINER_REQ="$REPO_ROOT/authorization_eva_sandbox/requirements-container.txt"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

if ! command -v "$PYTHON" &>/dev/null; then
  echo "ERROR: $PYTHON not found." >&2
  exit 1
fi

echo "[verify] python: $($PYTHON --version)"
echo "[verify] creating temp venv at $TMP/venv"
"$PYTHON" -m venv "$TMP/venv"
# shellcheck disable=SC1091
source "$TMP/venv/bin/activate"
pip install -q --upgrade pip wheel

echo "[verify] dry-run host requirements..."
if ! pip install --dry-run -r "$HOST_REQ" >/dev/null; then
  echo "ERROR: host requirements conflict (see pip output above)." >&2
  pip install --dry-run -r "$HOST_REQ" 2>&1 | tail -30
  exit 1
fi
echo "[verify] host requirements OK"

echo "[verify] dry-run container requirements..."
if ! pip install --dry-run -r "$CONTAINER_REQ" >/dev/null; then
  echo "ERROR: container requirements conflict (see pip output above)." >&2
  pip install --dry-run -r "$CONTAINER_REQ" 2>&1 | tail -30
  exit 1
fi
echo "[verify] container requirements OK"

echo "[verify] all dependency files resolve cleanly"
