#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEMPLATE_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

cd "$TEMPLATE_DIR"

env_value() {
  local key="$1"
  [[ -f .env ]] || return 1
  awk -F= -v key="$key" '
    $1 == key {
      sub(/^[^=]*=/, "")
      gsub(/^"|"$/, "")
      print
      exit
    }
  ' .env
}

API_PORT_VALUE="$(env_value API_PORT || true)"
CONSOLE_PORT_VALUE="$(env_value CONSOLE_PORT || true)"
API_URL_VALUE="$(env_value API_URL || true)"
CONSOLE_URL_VALUE="$(env_value CONSOLE_URL || true)"

API_URL="${API_URL_VALUE:-http://localhost:${API_PORT_VALUE:-8100}}"
CONSOLE_URL="${CONSOLE_URL_VALUE:-http://localhost:${CONSOLE_PORT_VALUE:-3000}}"

printf 'API: '
curl -fsS "$API_URL/health" >/dev/null
printf 'ok\n'

printf 'Console: '
curl -fsS "$CONSOLE_URL" >/dev/null
printf 'ok\n'

printf 'DB: '
docker compose exec -T api python - <<'PY'
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

db_path = Path(os.environ.get("PIR_DB_PATH", "/data/pir/console.db"))
if not db_path.exists():
    raise SystemExit(f"missing database: {db_path}")

with sqlite3.connect(db_path) as conn:
    version = conn.execute("SELECT MAX(version) FROM schema_versions").fetchone()[0]
    users = conn.execute("SELECT COUNT(*) FROM users WHERE deleted_at IS NULL").fetchone()[0]

print(f"ok version={version} users={users}")
PY
