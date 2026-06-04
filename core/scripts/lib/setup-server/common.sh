# shellcheck shell=bash
# core/scripts/lib/setup-server/common.sh
#
# Shared primitives for setup-server modular libs:
#   - logging (log/warn/fail) with optional structured step counter
#   - dry-run aware command wrappers (run/run_root/run_root_sensitive/run_as_deploy_user)
#   - status probes (deploy_user_status/root_status)
#   - command + systemd unit detection
#
# Sourced by core/scripts/setup-server.sh and other lib/*.sh modules.

# Step counter for verbose logging. Each module increments it via mark_step().
SETUP_STEP_COUNT="${SETUP_STEP_COUNT:-0}"
SETUP_STEP_TOTAL="${SETUP_STEP_TOTAL:-0}"

# --------------------------------------------------------------------------
# Logging
# --------------------------------------------------------------------------

is_verbose() {
  [[ "${MARVIS_VERBOSE:-0}" == "1" ]]
}

mark_step() {
  # Caller passes a short module name (e.g. "os-detect", "pkg-apt").
  SETUP_STEP_COUNT=$((SETUP_STEP_COUNT + 1))
  SETUP_CURRENT_MODULE="$1"
}

log() {
  if is_verbose && [[ -n "${SETUP_CURRENT_MODULE:-}" ]]; then
    printf '[setup-server] [%02d/%02d] [%s] %s\n' \
      "$SETUP_STEP_COUNT" "$SETUP_STEP_TOTAL" "$SETUP_CURRENT_MODULE" "$*"
  else
    printf '[setup-server] %s\n' "$*"
  fi
}

warn() {
  printf '[setup-server] WARN: %s\n' "$*" >&2
}

fail() {
  printf '[setup-server] ERROR: %s\n' "$*" >&2
  exit 1
}

# --------------------------------------------------------------------------
# Dry-run aware execution
# --------------------------------------------------------------------------

is_dry_run() {
  [[ "${MARVIS_DRY_RUN:-0}" == "1" ]]
}

print_command() {
  printf '[setup-server] dry-run:'
  printf ' %q' "$@"
  printf '\n'
}

run() {
  if is_dry_run; then
    print_command "$@"
    return 0
  fi
  "$@"
}

run_root() {
  run "${ROOT_CMD[@]}" "$@"
}

run_root_sensitive() {
  # Mask sensitive args (e.g. tokens) in dry-run output.
  local display="$1"
  shift
  if is_dry_run; then
    printf '[setup-server] dry-run: %s\n' "$display"
    return 0
  fi
  "${ROOT_CMD[@]}" "$@"
}

run_as_deploy_user() {
  if is_dry_run; then
    if [[ "${EUID}" -eq 0 ]]; then
      print_command runuser -u "$MARVIS_DEPLOY_USER" -- "$@"
    else
      print_command sudo -u "$MARVIS_DEPLOY_USER" "$@"
    fi
    return 0
  fi

  if [[ "${EUID}" -eq 0 ]]; then
    runuser -u "$MARVIS_DEPLOY_USER" -- "$@"
  else
    sudo -u "$MARVIS_DEPLOY_USER" "$@"
  fi
}

deploy_user_status() {
  if is_dry_run; then
    return 1
  fi

  if [[ "${EUID}" -eq 0 ]]; then
    runuser -u "$MARVIS_DEPLOY_USER" -- "$@"
  else
    sudo -u "$MARVIS_DEPLOY_USER" "$@"
  fi
}

root_status() {
  if is_dry_run; then
    return 1
  fi
  "${ROOT_CMD[@]}" "$@"
}

# --------------------------------------------------------------------------
# Probes
# --------------------------------------------------------------------------

command_exists() {
  command -v "$1" >/dev/null 2>&1
}

systemd_unit_exists() {
  local unit="$1"

  if is_dry_run || ! command_exists systemctl; then
    return 1
  fi

  "${ROOT_CMD[@]}" systemctl list-unit-files --type=service "$unit" 2>/dev/null \
    | awk '{print $1}' \
    | grep -qx "$unit"
}
