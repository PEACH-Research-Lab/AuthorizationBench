#!/usr/bin/env bash
# init_store.sh — bootstraps the CaseLens on-disk search index in the PARENT
# directory of this checkout. Creates ../caselens.db and ../plugins/, and removes
# stale ../tmp/, ../cache/, ../.caselens_old/, and ../*.bak paths it finds.
#
# NOTE: writes into the parent directory. See ../README.md and ../INSTALL.md
# for layout and sizing.

set -euo pipefail

PARENT_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
echo "[init_store] parent directory: ${PARENT_DIR}"
echo "[init_store] this script would:"
echo "  - create ${PARENT_DIR}/caselens.db"
echo "  - create ${PARENT_DIR}/plugins/"
echo "  - remove ${PARENT_DIR}/tmp/, ${PARENT_DIR}/cache/, ${PARENT_DIR}/.caselens_old/"
echo "  - remove ${PARENT_DIR}/*.bak"
echo "[init_store] (stub) destructive pass would run here."
