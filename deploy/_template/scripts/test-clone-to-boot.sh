#!/usr/bin/env bash
# Acceptance test: simulates the OSS user journey of cloning the repo, copying
# deploy/_template/ to a working directory, editing .env with test values, then
# booting the full stack and probing /health + admin login. Used by the CI
# workflow .github/workflows/template-boot-test.yml.
#
# Usage:
#   bash deploy/_template/scripts/test-clone-to-boot.sh [--source PATH]
#
#   --source PATH   Root of the marvis repository to test against. Defaults to
#                   the repo root detected from this script's path. CI passes
#                   "." to test the checked-out branch instead of the upstream
#                   default branch.
#
# Exit code: 0 on success, non-zero on failure. The script always tears down
# the Compose project before exiting (success or failure).

set -euo pipefail

SOURCE=""
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEFAULT_SOURCE="$(cd "$SCRIPT_DIR/../../.." && pwd)"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --source)
      SOURCE="$2"
      shift 2
      ;;
    --source=*)
      SOURCE="${1#--source=}"
      shift
      ;;
    -h|--help)
      sed -n '2,15p' "$0"
      exit 0
      ;;
    *)
      printf 'Unknown argument: %s\n' "$1" >&2
      exit 64
      ;;
  esac
done

SOURCE="${SOURCE:-$DEFAULT_SOURCE}"
SOURCE="$(cd "$SOURCE" && pwd)"

if [[ ! -d "$SOURCE/deploy/_template" ]]; then
  printf 'error: %s does not contain deploy/_template/\n' "$SOURCE" >&2
  exit 65
fi

WORK_ROOT="$(mktemp -d -t marvis-test-clone-XXXXXX)"
PROJECT_NAME="marvis-test-$(basename "$WORK_ROOT" | tr -dc 'a-z0-9')"
INSTANCE_DIR="$WORK_ROOT/instance"
LOG_DIR="$WORK_ROOT/logs"
ADMIN_PASSWORD="test-$(head -c 16 /dev/urandom | base64 | tr -dc 'a-zA-Z0-9' | head -c 24)"
JWT_SECRET="$(head -c 48 /dev/urandom | base64 | tr -dc 'a-zA-Z0-9' | head -c 64)"

mkdir -p "$LOG_DIR"

log() {
  printf '[test-clone-to-boot] %s\n' "$*"
}

cleanup() {
  local exit_code=$?
  log "tearing down Compose project $PROJECT_NAME"
  (cd "$INSTANCE_DIR" 2>/dev/null && \
    docker compose -p "$PROJECT_NAME" down -v --remove-orphans >>"$LOG_DIR/teardown.log" 2>&1) || true
  if (( exit_code != 0 )); then
    log "FAILED — logs preserved at $LOG_DIR"
    if [[ -f "$LOG_DIR/compose-up.log" ]]; then
      log "--- last 80 lines of compose-up.log ---"
      tail -n 80 "$LOG_DIR/compose-up.log" || true
    fi
    if [[ -f "$LOG_DIR/services.log" ]]; then
      log "--- last 80 lines of services.log ---"
      tail -n 80 "$LOG_DIR/services.log" || true
    fi
  else
    rm -rf "$WORK_ROOT"
  fi
  exit "$exit_code"
}
trap cleanup EXIT INT TERM

log "source repo:    $SOURCE"
log "scratch dir:    $WORK_ROOT"
log "compose project: $PROJECT_NAME"

# Stage instance: copy deploy/_template/ verbatim. We deliberately do NOT copy
# the whole repo — only the template — because the OSS user journey starts with
# `cp -r deploy/_template my-marvis` and points the build context at the source
# tree via the `context: ../..` paths inside docker-compose.yml.
mkdir -p "$INSTANCE_DIR"
cp -R "$SOURCE/deploy/_template/." "$INSTANCE_DIR/"

# Rewrite compose context paths to absolute SOURCE root so the test instance can
# live anywhere on disk (CI runners use /tmp).
python3 - "$INSTANCE_DIR/docker-compose.yml" "$SOURCE" <<'PY'
import pathlib, re, sys
compose_path = pathlib.Path(sys.argv[1])
source_root = pathlib.Path(sys.argv[2]).resolve()
text = compose_path.read_text()
text = text.replace("context: ../..", f"context: {source_root}")
text = text.replace("context: ../../core/console", f"context: {source_root}/core/console")
text = text.replace(
    "dockerfile: deploy/_template/Dockerfile.api",
    "dockerfile: deploy/_template/Dockerfile.api",
)
compose_path.write_text(text)
PY

# Build .env with safe test values.
cp "$INSTANCE_DIR/.env.example" "$INSTANCE_DIR/.env"
python3 - "$INSTANCE_DIR/.env" "$ADMIN_PASSWORD" "$JWT_SECRET" "$PROJECT_NAME" <<'PY'
import pathlib, sys
env_path = pathlib.Path(sys.argv[1])
admin_password = sys.argv[2]
jwt_secret = sys.argv[3]
project = sys.argv[4]
overrides = {
    "COMPOSE_PROJECT_NAME": project,
    "PIR_PASSWORD": admin_password,
    "PIR_JWT_SECRET": jwt_secret,
    "PIR_ENV": "development",
    "MARVIS_ADMIN_EMAIL": "ci-test@example.local",
}
lines = env_path.read_text().splitlines()
seen = set()
out = []
for line in lines:
    if "=" in line and not line.lstrip().startswith("#"):
        key = line.split("=", 1)[0].strip()
        if key in overrides:
            out.append(f"{key}={overrides[key]}")
            seen.add(key)
            continue
    out.append(line)
for key in overrides:
    if key not in seen:
        out.append(f"{key}={overrides[key]}")
env_path.write_text("\n".join(out) + "\n")
PY

cd "$INSTANCE_DIR"

log "docker compose build + up (this may take several minutes on first run)"
docker compose -p "$PROJECT_NAME" up --build -d >"$LOG_DIR/compose-up.log" 2>&1

# Wait for /health on the API. Timeout 300s.
API_PORT="$(awk -F= '/^API_PORT=/{print $2; exit}' .env)"
API_PORT="${API_PORT:-8100}"
HEALTH_URL="http://localhost:${API_PORT}/health"

log "waiting for $HEALTH_URL (timeout 300s)"
SECONDS_WAITED=0
until curl -fsS "$HEALTH_URL" >/dev/null 2>&1; do
  if (( SECONDS_WAITED >= 300 )); then
    log "timeout waiting for $HEALTH_URL"
    docker compose -p "$PROJECT_NAME" logs --no-color >"$LOG_DIR/services.log" 2>&1 || true
    exit 75
  fi
  sleep 5
  SECONDS_WAITED=$((SECONDS_WAITED + 5))
done
log "API healthy after ${SECONDS_WAITED}s"

# Admin login check. The login endpoint returns a JWT on success; we don't
# parse it, only assert the HTTP status.
LOGIN_URL="http://localhost:${API_PORT}/api/v1/auth/login"
log "POST $LOGIN_URL"
HTTP_CODE="$(curl -sS -o "$LOG_DIR/login.json" -w '%{http_code}' \
  -H 'Content-Type: application/json' \
  -X POST "$LOGIN_URL" \
  -d "{\"username\":\"admin\",\"password\":\"$ADMIN_PASSWORD\"}" || true)"

if [[ "$HTTP_CODE" != "200" ]]; then
  log "admin login failed: HTTP $HTTP_CODE"
  docker compose -p "$PROJECT_NAME" logs --no-color >"$LOG_DIR/services.log" 2>&1 || true
  exit 76
fi

log "admin login OK"
log "PASS"
exit 0
