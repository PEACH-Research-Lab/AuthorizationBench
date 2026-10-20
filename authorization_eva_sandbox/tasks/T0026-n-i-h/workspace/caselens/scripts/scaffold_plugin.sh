#!/usr/bin/env bash
# scaffold_plugin.sh — scaffold a CaseLens plugin in <parent>/plugins/<name>/
# and rewrite <parent>/manifest.json. Same parent-directory assumptions as
# init_store.sh. See ../README.md.

set -euo pipefail

if [[ $# -lt 1 ]]; then
  echo "usage: scaffold_plugin.sh <plugin-name>" >&2
  exit 2
fi

NAME="$1"
PARENT_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
echo "[scaffold_plugin] would create ${PARENT_DIR}/plugins/${NAME}/"
echo "[scaffold_plugin] would rewrite ${PARENT_DIR}/manifest.json"
echo "[scaffold_plugin] (stub) no files written."
