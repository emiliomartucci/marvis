#!/usr/bin/env bash

set -euo pipefail

VERSION="0.1.0"
DEFAULT_DEPLOY_DIR="./marvis-deploy"
DEFAULT_DOMAIN="yourcompany.marvisx.io"
DEFAULT_EMAIL="ops@example.com"
DEFAULT_CLOUD="bare-metal"
DEFAULT_TUNNEL_MODE="direct-ip"
DEFAULT_DEPLOY_USER="marvis"
DOCS_URL="https://justaskmarvis.com/oss"

SOURCE="${BASH_SOURCE[0]}"
while [[ -L "$SOURCE" ]]; do
  SOURCE_DIR="$(cd -P "$(dirname "$SOURCE")" && pwd)"
  SOURCE="$(readlink "$SOURCE")"
  [[ "$SOURCE" != /* ]] && SOURCE="$SOURCE_DIR/$SOURCE"
done
SCRIPT_DIR="$(cd -P "$(dirname "$SOURCE")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
TEMPLATE_DIR="$REPO_ROOT/deploy/_template"
SETUP_SERVER_SCRIPT="$REPO_ROOT/core/scripts/setup-server.sh"

DOMAIN=""
EMAIL=""
CLOUD=""
TUNNEL_MODE=""
DEPLOY_DIR="$DEFAULT_DEPLOY_DIR"
DRY_RUN=0

log() {
  printf '[marvis init] %s\n' "$*"
}

warn() {
  printf '[marvis init] WARN: %s\n' "$*" >&2
}

fail() {
  printf '[marvis init] ERROR: %s\n' "$*" >&2
  exit 1
}

root_usage() {
  cat <<'EOF'
Usage:
  marvis init [--domain X] [--email Y] [--cloud hetzner|aws|do|linode|bare-metal] [--dry-run]

Commands:
  init    Bootstrap a Marvis deploy from deploy/_template.

Run `marvis init --help` for init options.
EOF
}

init_usage() {
  cat <<'EOF'
Usage:
  marvis init [options]
  core/scripts/marvis-init.sh [options]

Options:
  --domain DOMAIN          Public hostname, for example yourcompany.marvisx.io.
  --email EMAIL            Technical contact email for certificates and alerts.
  --cloud PROVIDER         hetzner, aws, do, linode, or bare-metal.
  --tunnel-mode MODE       cf-tunnel, ssh-port-forward, or direct-ip.
  --deploy-dir DIR         Target deploy directory. Default: ./marvis-deploy.
  --dry-run                Print the bootstrap plan without copying or running commands.
  -h, --help               Show this help.

Environment:
  MARVIS_CLOUDFLARE_TUNNEL_TOKEN   Required when --tunnel-mode cf-tunnel is used.
  MARVIS_DEPLOY_USER               Deploy Linux user passed to setup-server.sh. Default: marvis.
  MARVIS_SSH_PUBLIC_KEY            Optional SSH public key passed to setup-server.sh.
  MARVIS_ENABLE_UFW                Passed through to setup-server.sh. Default: 1.
EOF
}

is_interactive() {
  [[ -t 0 ]]
}

is_dry_run() {
  [[ "$DRY_RUN" == "1" ]]
}

redact() {
  local value="$1"
  if [[ -z "$value" ]]; then
    printf '<empty>'
  else
    printf '<redacted>'
  fi
}

require_arg() {
  local name="$1"
  local value="${2:-}"
  [[ -n "$value" ]] || fail "$name requires a value."
}

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --domain)
        require_arg "$1" "${2:-}"
        DOMAIN="$2"
        shift 2
        ;;
      --domain=*)
        DOMAIN="${1#*=}"
        shift
        ;;
      --email)
        require_arg "$1" "${2:-}"
        EMAIL="$2"
        shift 2
        ;;
      --email=*)
        EMAIL="${1#*=}"
        shift
        ;;
      --cloud)
        require_arg "$1" "${2:-}"
        CLOUD="$2"
        shift 2
        ;;
      --cloud=*)
        CLOUD="${1#*=}"
        shift
        ;;
      --tunnel-mode)
        require_arg "$1" "${2:-}"
        TUNNEL_MODE="$2"
        shift 2
        ;;
      --tunnel-mode=*)
        TUNNEL_MODE="${1#*=}"
        shift
        ;;
      --deploy-dir)
        require_arg "$1" "${2:-}"
        DEPLOY_DIR="$2"
        shift 2
        ;;
      --deploy-dir=*)
        DEPLOY_DIR="${1#*=}"
        shift
        ;;
      --dry-run)
        DRY_RUN=1
        shift
        ;;
      -h|--help)
        init_usage
        exit 0
        ;;
      *)
        fail "Unknown option: $1"
        ;;
    esac
  done
}

prompt_value() {
  local label="$1"
  local default_value="$2"
  local value

  printf '%s [%s]: ' "$label" "$default_value" >&2
  read -r value
  printf '%s' "${value:-$default_value}"
}

prompt_choice() {
  local label="$1"
  local default_value="$2"
  shift 2
  local choices=("$@")
  local value

  while true; do
    printf '%s (%s) [%s]: ' "$label" "$(IFS='|'; printf '%s' "${choices[*]}")" "$default_value" >&2
    read -r value
    value="${value:-$default_value}"
    if contains "$value" "${choices[@]}"; then
      printf '%s' "$value"
      return 0
    fi
    warn "Choose one of: ${choices[*]}"
  done
}

contains() {
  local needle="$1"
  shift
  local value
  for value in "$@"; do
    [[ "$value" == "$needle" ]] && return 0
  done
  return 1
}

resolve_config() {
  if [[ -z "$DOMAIN" ]]; then
    if is_interactive; then
      DOMAIN="$(prompt_value "Domain target" "$DEFAULT_DOMAIN")"
    else
      fail "Missing --domain in non-interactive mode."
    fi
  fi

  if [[ -z "$EMAIL" ]]; then
    if is_interactive; then
      EMAIL="$(prompt_value "Email contact" "$DEFAULT_EMAIL")"
    else
      fail "Missing --email in non-interactive mode."
    fi
  fi

  if [[ -z "$CLOUD" ]]; then
    if is_interactive; then
      CLOUD="$(prompt_choice "Cloud provider" "$DEFAULT_CLOUD" hetzner aws do linode bare-metal)"
    else
      CLOUD="$DEFAULT_CLOUD"
    fi
  fi

  if [[ -z "$TUNNEL_MODE" ]]; then
    if is_interactive; then
      TUNNEL_MODE="$(prompt_choice "Tunnel mode" "$DEFAULT_TUNNEL_MODE" cf-tunnel ssh-port-forward direct-ip)"
    else
      TUNNEL_MODE="$DEFAULT_TUNNEL_MODE"
    fi
  fi
}

validate_config() {
  [[ -d "$TEMPLATE_DIR" ]] || fail "Deploy template not found: $TEMPLATE_DIR"
  [[ -x "$SETUP_SERVER_SCRIPT" || -r "$SETUP_SERVER_SCRIPT" ]] || fail "setup-server.sh not found: $SETUP_SERVER_SCRIPT"

  [[ "$DOMAIN" != http://* && "$DOMAIN" != https://* ]] || fail "--domain must be a hostname, not a URL."
  [[ "$DOMAIN" != */* ]] || fail "--domain must not contain a path."
  [[ "$EMAIL" == *@* ]] || fail "--email must look like an email address."
  contains "$CLOUD" hetzner aws do linode bare-metal || fail "--cloud must be one of: hetzner aws do linode bare-metal."
  contains "$TUNNEL_MODE" cf-tunnel ssh-port-forward direct-ip || fail "--tunnel-mode must be one of: cf-tunnel ssh-port-forward direct-ip."

  if [[ "$TUNNEL_MODE" == "cf-tunnel" && -z "${MARVIS_CLOUDFLARE_TUNNEL_TOKEN:-}" ]]; then
    fail "cf-tunnel requires MARVIS_CLOUDFLARE_TUNNEL_TOKEN in the environment."
  fi
}

install_hint() {
  case "$1" in
    docker)
      printf 'Install Docker Engine, then rerun this command. On Ubuntu/Debian, setup-server.sh can install it.\n'
      ;;
    docker-compose-plugin)
      printf 'Install the Docker Compose plugin, for example: sudo apt-get install docker-compose-plugin\n'
      ;;
    git)
      printf 'Install git, for example: sudo apt-get install git\n'
      ;;
    jq)
      printf 'Install jq, for example: sudo apt-get install jq\n'
      ;;
    curl)
      printf 'Install curl, for example: sudo apt-get install curl\n'
      ;;
    python3)
      printf 'Install python3, for example: sudo apt-get install python3\n'
      ;;
    *)
      printf 'Install %s and rerun this command.\n' "$1"
      ;;
  esac
}

preflight_checks() {
  local missing=()
  local dep

  for dep in docker git jq curl python3; do
    if ! command -v "$dep" >/dev/null 2>&1; then
      missing+=("$dep")
    fi
  done

  if command -v docker >/dev/null 2>&1; then
    if ! docker compose version >/dev/null 2>&1; then
      missing+=("docker-compose-plugin")
    fi
  fi

  if [[ ${#missing[@]} -gt 0 ]]; then
    printf '[marvis init] Missing dependencies:\n' >&2
    for dep in "${missing[@]}"; do
      printf '  - %s: ' "$dep" >&2
      install_hint "$dep" >&2
    done
    exit 1
  fi

  log "Pre-flight checks passed."
}

domain_slug() {
  python3 - "$DOMAIN" <<'PY'
import re
import sys

domain = sys.argv[1].lower()
slug = re.sub(r"[^a-z0-9_-]+", "-", domain).strip("-_")
if not slug:
    slug = "local"
if not re.match(r"^[a-z0-9]", slug):
    slug = "marvis-" + slug
print(f"marvis-{slug}"[:63].rstrip("-_"))
PY
}

public_scheme() {
  if [[ "$TUNNEL_MODE" == "cf-tunnel" ]]; then
    printf 'https'
  else
    printf 'http'
  fi
}

ws_scheme() {
  if [[ "$TUNNEL_MODE" == "cf-tunnel" ]]; then
    printf 'wss'
  else
    printf 'ws'
  fi
}

public_console_url() {
  if [[ "$TUNNEL_MODE" == "ssh-port-forward" ]]; then
    printf 'http://localhost:3000'
  else
    printf '%s://%s' "$(public_scheme)" "$DOMAIN"
  fi
}

public_api_url() {
  if [[ "$TUNNEL_MODE" == "ssh-port-forward" ]]; then
    printf 'http://localhost:8100'
  else
    printf '%s://%s' "$(public_scheme)" "$DOMAIN"
  fi
}

public_ws_url() {
  if [[ "$TUNNEL_MODE" == "ssh-port-forward" ]]; then
    printf 'ws://localhost:8100'
  else
    printf '%s://%s' "$(ws_scheme)" "$DOMAIN"
  fi
}

public_probe_url() {
  if [[ "$TUNNEL_MODE" == "ssh-port-forward" ]]; then
    printf 'http://localhost:8100/health'
  else
    printf '%s://%s/healthz' "$(public_scheme)" "$DOMAIN"
  fi
}

copy_template() {
  log "Copying deploy template to $DEPLOY_DIR"
  mkdir -p "$DEPLOY_DIR"
  cp -R "$TEMPLATE_DIR"/. "$DEPLOY_DIR"/
}

render_env() {
  local env_example="$DEPLOY_DIR/.env.example"
  local env_file="$DEPLOY_DIR/.env"
  local compose_project
  local console_url
  local api_url
  local ws_url
  local probe_url

  compose_project="$(domain_slug)"
  console_url="$(public_console_url)"
  api_url="$(public_api_url)"
  ws_url="$(public_ws_url)"
  probe_url="$(public_probe_url)"

  log "Rendering $env_file"
  python3 - "$env_example" "$env_file" <<'PY'
from __future__ import annotations

import json
import os
import secrets
import sys
from pathlib import Path

env_example = Path(sys.argv[1])
env_file = Path(sys.argv[2])

domain = os.environ["MARVIS_INIT_DOMAIN"]
email = os.environ["MARVIS_INIT_EMAIL"]
cloud = os.environ["MARVIS_INIT_CLOUD"]
tunnel_mode = os.environ["MARVIS_INIT_TUNNEL_MODE"]
compose_project = os.environ["MARVIS_INIT_COMPOSE_PROJECT"]
console_url = os.environ["MARVIS_INIT_CONSOLE_URL"]
api_url = os.environ["MARVIS_INIT_API_URL"]
ws_url = os.environ["MARVIS_INIT_WS_URL"]
probe_url = os.environ["MARVIS_INIT_PROBE_URL"]
cf_token = os.environ.get("MARVIS_CLOUDFLARE_TUNNEL_TOKEN", "")

cors_origins = [console_url, "http://localhost:3000"]

replacements = {
    "COMPOSE_PROJECT_NAME": compose_project,
    "MARVIS_IMAGE_TAG": "local",
    "NGINX_PORT": "80" if tunnel_mode == "direct-ip" else "8080",
    "CLOUDFLARE_TUNNEL_TOKEN": cf_token if tunnel_mode == "cf-tunnel" else "",
    "PUBLIC_DOMAIN": domain,
    "ACME_EMAIL": email,
    "PIR_ENV": "production",
    "DEPLOY_MODE": "core",
    "PIR_INSTANCE": domain,
    "PIR_CANARY_BANNER": "false",
    "PIR_VOYAGE_DISABLED": "true",
    "PIR_JWT_SECRET": secrets.token_urlsafe(48),
    "PIR_PASSWORD": secrets.token_urlsafe(18),
    "TASKS_API_TOKEN": secrets.token_urlsafe(40),
    "COOKIE_DOMAIN": "" if tunnel_mode == "ssh-port-forward" else domain,
    "CORS_ORIGINS_PROD": json.dumps(cors_origins, separators=(",", ":")),
    "CONSOLE_BASE_URL": console_url,
    "GITHUB_WEBHOOK_SECRET": secrets.token_urlsafe(32),
    "LLM_GATEWAY_BASE_URL": f"{api_url}/v1",
    "LLM_GATEWAY_AUX_BASE_URL": api_url,
    "BRAIN_LLM_GATEWAY_BASE_URL": api_url,
    "NEXT_PUBLIC_API_URL": api_url,
    "NEXT_PUBLIC_WS_URL": ws_url,
    "NEXT_PUBLIC_DIRECT_WS_URL": ws_url,
    "NEXT_PUBLIC_DIRECT_WS_PROBE_URL": probe_url,
    "MARVIS_ADMIN_EMAIL": email,
}

extra = {
    "MARVIS_CLOUD_PROVIDER": cloud,
    "MARVIS_TUNNEL_MODE": tunnel_mode,
    "API_URL": "http://localhost:8100",
    "CONSOLE_URL": "http://localhost:3000",
}

lines: list[str] = []
seen: set[str] = set()

for line in env_example.read_text(encoding="utf-8").splitlines():
    if not line or line.lstrip().startswith("#") or "=" not in line:
        lines.append(line)
        continue

    key, _value = line.split("=", 1)
    if key in replacements:
        lines.append(f"{key}={replacements[key]}")
        seen.add(key)
    else:
        lines.append(line)

for key, value in extra.items():
    lines.append(f"{key}={value}")

missing = sorted(set(replacements) - seen)
if missing:
    lines.append("")
    lines.append("# Added by marvis init because these keys were not present in .env.example.")
    for key in missing:
        lines.append(f"{key}={replacements[key]}")

env_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
PY

  chmod 0600 "$env_file"
}

run_setup_server() {
  log "Running setup-server.sh for $CLOUD"
    MARVIS_DOMAIN="$DOMAIN" \
    MARVIS_EMAIL="$EMAIL" \
    MARVIS_CLOUD_PROVIDER="$CLOUD" \
    MARVIS_CLOUDFLARE_TUNNEL_TOKEN="${MARVIS_CLOUDFLARE_TUNNEL_TOKEN:-}" \
    MARVIS_DEPLOY_USER="${MARVIS_DEPLOY_USER:-$DEFAULT_DEPLOY_USER}" \
    MARVIS_SSH_PUBLIC_KEY="${MARVIS_SSH_PUBLIC_KEY:-}" \
    MARVIS_ENABLE_UFW="${MARVIS_ENABLE_UFW:-1}" \
    MARVIS_ENABLE_PASSWORDLESS_SUDO="${MARVIS_ENABLE_PASSWORDLESS_SUDO:-0}" \
    MARVIS_BASE_DIR="${MARVIS_BASE_DIR:-/opt/marvis}" \
    MARVIS_START_TMUX="${MARVIS_START_TMUX:-1}" \
    MARVIS_TMUX_DIR="${MARVIS_TMUX_DIR:-/var/lib/marvis/tmux}" \
    bash "$SETUP_SERVER_SCRIPT"
}

compose_args() {
  if [[ "$TUNNEL_MODE" == "cf-tunnel" ]]; then
    printf '%s\n' --profile tunnel
  fi
}

run_compose() {
  local compose_profile=()
  mapfile -t compose_profile < <(compose_args)

  log "Pulling Docker images"
  (cd "$DEPLOY_DIR" && docker compose "${compose_profile[@]}" pull --ignore-buildable)

  log "Starting Docker Compose stack"
  (cd "$DEPLOY_DIR" && docker compose "${compose_profile[@]}" up -d --build)
}

wait_for_health() {
  local api_health="http://localhost:8100/health"
  local console_health="http://localhost:3000"
  local deadline=$((SECONDS + 60))
  local ok=0

  log "Waiting for API and Console health checks (60s timeout)"
  while [[ $SECONDS -lt $deadline ]]; do
    if curl -fsS "$api_health" >/dev/null 2>&1 && curl -fsS "$console_health" >/dev/null 2>&1; then
      ok=1
      break
    fi
    sleep 3
  done

  [[ "$ok" == "1" ]] || fail "Healthcheck timeout after 60s. Check logs with: cd $DEPLOY_DIR && docker compose logs --tail=100"
  log "Health checks passed."
}

print_dry_run() {
  cat <<EOF
[marvis init] Dry run plan
  repo root:      $REPO_ROOT
  template:       $TEMPLATE_DIR
  deploy dir:     $DEPLOY_DIR
  domain:         $DOMAIN
  email:          $EMAIL
  cloud:          $CLOUD
  tunnel mode:    $TUNNEL_MODE
  cf token:       $(redact "${MARVIS_CLOUDFLARE_TUNNEL_TOKEN:-}")

Would run:
  1. Pre-flight dependency checks
  2. Copy deploy/_template/ to $DEPLOY_DIR
  3. Render $DEPLOY_DIR/.env
  4. Run core/scripts/setup-server.sh with MARVIS_DOMAIN, MARVIS_EMAIL, and MARVIS_CLOUD_PROVIDER
  5. Run docker compose pull --ignore-buildable
  6. Run docker compose up -d --build
  7. Wait up to 60s for API and Console health checks

Expected URLs:
  Console: $(public_console_url)
  API:     $(public_api_url)

SSH reconnect:
  ssh ${MARVIS_DEPLOY_USER:-$DEFAULT_DEPLOY_USER}@$DOMAIN

Docs:
  $DOCS_URL
EOF

  if [[ "$TUNNEL_MODE" == "ssh-port-forward" ]]; then
    cat <<EOF

SSH port forward:
  ssh -L 3000:localhost:3000 -L 8100:localhost:8100 ${MARVIS_DEPLOY_USER:-$DEFAULT_DEPLOY_USER}@$DOMAIN
EOF
  fi
}

print_final_output() {
  cat <<EOF

Marvis deploy is booted.

Console: $(public_console_url)
API:     $(public_api_url)

SSH reconnect:
  ssh ${MARVIS_DEPLOY_USER:-$DEFAULT_DEPLOY_USER}@$DOMAIN

Deploy directory:
  $DEPLOY_DIR

Docs:
  $DOCS_URL
EOF

  if [[ "$TUNNEL_MODE" == "ssh-port-forward" ]]; then
    cat <<EOF

SSH port forward:
  ssh -L 3000:localhost:3000 -L 8100:localhost:8100 ${MARVIS_DEPLOY_USER:-$DEFAULT_DEPLOY_USER}@$DOMAIN
EOF
  fi
}

init_main() {
  parse_args "$@"
  resolve_config
  validate_config
  preflight_checks

  if is_dry_run; then
    print_dry_run
    return 0
  fi

  MARVIS_INIT_DOMAIN="$DOMAIN" \
    MARVIS_INIT_EMAIL="$EMAIL" \
    MARVIS_INIT_CLOUD="$CLOUD" \
    MARVIS_INIT_TUNNEL_MODE="$TUNNEL_MODE" \
    MARVIS_INIT_COMPOSE_PROJECT="$(domain_slug)" \
    MARVIS_INIT_CONSOLE_URL="$(public_console_url)" \
    MARVIS_INIT_API_URL="$(public_api_url)" \
    MARVIS_INIT_WS_URL="$(public_ws_url)" \
    MARVIS_INIT_PROBE_URL="$(public_probe_url)" \
    copy_template

  MARVIS_INIT_DOMAIN="$DOMAIN" \
    MARVIS_INIT_EMAIL="$EMAIL" \
    MARVIS_INIT_CLOUD="$CLOUD" \
    MARVIS_INIT_TUNNEL_MODE="$TUNNEL_MODE" \
    MARVIS_INIT_COMPOSE_PROJECT="$(domain_slug)" \
    MARVIS_INIT_CONSOLE_URL="$(public_console_url)" \
    MARVIS_INIT_API_URL="$(public_api_url)" \
    MARVIS_INIT_WS_URL="$(public_ws_url)" \
    MARVIS_INIT_PROBE_URL="$(public_probe_url)" \
    render_env

  run_setup_server
  run_compose
  wait_for_health
  print_final_output
}

main() {
  local invoked_as
  invoked_as="$(basename "$0")"

  if [[ "$invoked_as" == "marvis" ]]; then
    case "${1:-}" in
      init)
        shift
        init_main "$@"
        ;;
      -h|--help|"")
        root_usage
        ;;
      *)
        fail "Unknown command: ${1:-}"
        ;;
    esac
    return 0
  fi

  if [[ "${1:-}" == "init" ]]; then
    shift
  fi
  init_main "$@"
}

main "$@"
