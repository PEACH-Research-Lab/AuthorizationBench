#!/usr/bin/env bash
# apply_patches.sh
#
# Apply the OpenHands patches required to capture model-side reasoning data
# (reasoning_tokens, reasoning_content, thinking_blocks). Idempotent: re-running
# is a no-op if the patches are already applied (patch -R --dry-run check).
#
# Target: the `openhands` package installed inside the project's virtualenv.
# Default expects ./au312 venv at the repo root (matches our setup).
#
# Usage:
#   bash apply_patches.sh [--site-packages /path/to/openhands]
#   bash apply_patches.sh --revert   # restore originals from .backup/

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../../.." && pwd)"
DEFAULT_OH="$REPO_ROOT/au312/lib/python3.12/site-packages/openhands"
OH_DIR="${OH_DIR:-$DEFAULT_OH}"
ACTION="apply"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --site-packages) OH_DIR="$2/openhands"; shift 2;;
    --revert)        ACTION="revert"; shift;;
    -h|--help)
      sed -n '2,18p' "$0"; exit 0;;
    *) echo "unknown arg: $1" >&2; exit 2;;
  esac
done

if [[ ! -d "$OH_DIR" ]]; then
  echo "ERROR: openhands directory not found: $OH_DIR" >&2
  echo "  Pass --site-packages /abs/path/to/site-packages to override." >&2
  exit 1
fi

PATCHES=(
  "01-metrics-add-reasoning-tokens.patch"
  "02-llm-extract-reasoning-tokens.patch"
  "03-function-calling-reasoning-content.patch"
)

if [[ "$ACTION" == "revert" ]]; then
  echo "[revert] restoring originals from .backup/ -> $OH_DIR"
  cp "$SCRIPT_DIR/.backup/metrics.py.orig"          "$OH_DIR/llm/metrics.py"
  cp "$SCRIPT_DIR/.backup/llm.py.orig"              "$OH_DIR/llm/llm.py"
  cp "$SCRIPT_DIR/.backup/function_calling.py.orig" "$OH_DIR/agenthub/codeact_agent/function_calling.py"
  echo "[revert] done."
  exit 0
fi

echo "[apply] target: $OH_DIR"
cd "$OH_DIR"

for p in "${PATCHES[@]}"; do
  patch_path="$SCRIPT_DIR/$p"
  # First check if already applied (reverse dry-run succeeds clean).
  if patch -R -p1 --dry-run --silent --force < "$patch_path" >/dev/null 2>&1; then
    echo "[skip ] $p (already applied)"
    continue
  fi
  # Otherwise try forward apply with --forward so it errors instead of asking
  # to reverse, and -N to skip already-applied hunks defensively.
  if patch -p1 -N --forward --silent < "$patch_path" >/dev/null 2>&1; then
    echo "[apply] $p"
  else
    echo "[FAIL ] $p does not apply cleanly. Manual intervention required." >&2
    exit 1
  fi
done

echo
echo "[apply] OpenHands patches done."
echo
echo "Verify with:"
echo "  python -c \"from openhands.llm.metrics import TokenUsage; print('reasoning_tokens' in TokenUsage.model_fields)\""
echo
echo "NOTE: run_eval.py changes (Patch 4) live in this repo and are tracked by git;"
echo "      no separate patch step is needed for them."
