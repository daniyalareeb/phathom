#!/usr/bin/env bash
# Start local SurrealDB (SPEC §11 Phase 2). Binds to loopback only.
# Usage: scripts/run_db.sh
set -euo pipefail

SURREAL_BIN="$(command -v surreal 2>/dev/null || echo "$HOME/.surrealdb/surreal")"
if [ ! -x "${SURREAL_BIN}" ]; then
  echo "surreal binary not found. Install: curl -sSf https://install.surrealdb.com | sh" >&2
  exit 1
fi

SURREAL_USER="${SURREAL_USER:-root}"
SURREAL_PASS="${SURREAL_PASS:-root}"

mkdir -p data/surreal
exec "${SURREAL_BIN}" start --bind 127.0.0.1:8000 \
  --user "${SURREAL_USER}" --pass "${SURREAL_PASS}" \
  surrealkv://data/surreal
