#!/usr/bin/env bash
# Bootstrap host venv for authorization_eva_sandbox eval harness.
# Run from anywhere: bash scripts/bootstrap.sh
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

PYTHON="${PYTHON:-python3.12}"
if ! command -v "$PYTHON" &>/dev/null; then
  echo "ERROR: $PYTHON not found. Install Python 3.12 first (see README.md)." >&2
  exit 1
fi

VENV="$REPO_ROOT/au312"
HOST_REQ="$REPO_ROOT/authorization_eva_sandbox/requirements-host.txt"
PATCHES="$REPO_ROOT/authorization_eva_sandbox/evaluation/reasoning_count/patches/apply_patches.sh"

echo "[bootstrap] repo: $REPO_ROOT"
echo "[bootstrap] python: $($PYTHON --version)"

if [[ ! -d "$VENV" ]]; then
  echo "[bootstrap] creating venv at $VENV"
  "$PYTHON" -m venv "$VENV"
else
  echo "[bootstrap] reusing existing venv at $VENV"
fi

# shellcheck disable=SC1091
source "$VENV/bin/activate"
pip install --upgrade pip wheel
pip install -r "$HOST_REQ"

# OpenHands runtime image build copies site-packages/skills/; pip does not create it.
SP="$(python -c 'import site; print(site.getsitepackages()[0])')"
mkdir -p "$SP/skills"

echo "[bootstrap] applying OpenHands reasoning patches"
bash "$PATCHES"

python -c "import openhands, litellm; print('[bootstrap] imports ok')"
pip check 2>/dev/null || echo "[bootstrap] warning: pip check reported issues (often harmless on upgraded venvs)"

CONFIG="$REPO_ROOT/authorization_eva_sandbox/evaluation/config.toml"
EXAMPLE="$REPO_ROOT/authorization_eva_sandbox/evaluation/config.toml.example"
if [[ ! -f "$CONFIG" ]]; then
  echo ""
  echo "[bootstrap] MISSING: authorization_eva_sandbox/evaluation/config.toml"
  if [[ -f "$EXAMPLE" ]]; then
    echo "  cp authorization_eva_sandbox/evaluation/config.toml.example \\"
    echo "     authorization_eva_sandbox/evaluation/config.toml"
    echo "  Then replace YOUR_* placeholders with real API keys."
  else
    echo "  Ask a teammate for config.toml (gitignored; never commit secrets)."
  fi
else
  echo "[bootstrap] found evaluation/config.toml"
fi

echo ""
echo "[bootstrap] done. Next steps (see README.md):"
echo "  source au312/bin/activate"
echo "  ./au312/bin/python -m unittest discover -s authorization_eva_sandbox/tests -p 'test_*.py' -q"
echo "  docker build -f authorization_eva_sandbox/Dockerfile.v2 -t auth-sandbox:v2 authorization_eva_sandbox"
echo "  ./au312/bin/python authorization_eva_sandbox/evaluation/run_task.py --help"
