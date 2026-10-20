#!/usr/bin/env bash
# deploy.sh — recommended production path: Docker Compose.
# First run pulls fieldvault/runtime (~4–6 GB) and needs ~15–20 GB free
# on this volume for extraction + volumes. See ../INSTALL.md.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CHECKOUT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
PARENT_DIR="$(cd "${CHECKOUT_DIR}/.." && pwd)"

echo "[deploy] checkout: ${CHECKOUT_DIR}"
echo "[deploy] parent volume: ${PARENT_DIR}"
echo "[deploy] would run: docker compose -f ${CHECKOUT_DIR}/docker-compose.yml up -d"
echo "[deploy] (stub) first pull of fieldvault/runtime:latest (~4.6 GB compressed)"
echo "[deploy] (stub) ensure ~15–20 GB free on ${PARENT_DIR} before pull"
