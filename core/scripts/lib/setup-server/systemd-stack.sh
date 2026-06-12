# shellcheck shell=bash
# core/scripts/lib/setup-server/systemd-stack.sh
#
# Opt-in systemd unit (marvis-stack.service) that auto-starts the Docker
# compose stack at boot. OSS users who do not have deploy-all.sh.
#
# Activated by MARVIS_ENABLE_SYSTEMD_STACK=1. Default off so we never override
# an existing systemd config silently.

ensure_systemd_stack() {
  mark_step "systemd-stack"

  if [[ "${MARVIS_ENABLE_SYSTEMD_STACK:-0}" != "1" ]]; then
    log "systemd auto-start disabled (set MARVIS_ENABLE_SYSTEMD_STACK=1 to enable)."
    return 0
  fi

  if ! command_exists systemctl; then
    warn "systemctl not available; cannot install marvis-stack.service."
    return 0
  fi

  local compose_file="${MARVIS_COMPOSE_FILE:-${MARVIS_BASE_DIR}/marvisx/docker-compose.yml}"
  local unit_path="/etc/systemd/system/marvis-stack.service"

  local unit_content
  unit_content="$(cat <<EOF
[Unit]
Description=Marvis docker compose stack
After=docker.service network-online.target
Requires=docker.service
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory=$(dirname "$compose_file")
ExecStart=/usr/bin/docker compose -f $compose_file up -d
ExecStop=/usr/bin/docker compose -f $compose_file down
TimeoutStartSec=300

[Install]
WantedBy=multi-user.target
EOF
)"

  if is_dry_run; then
    printf '[setup-server] dry-run: would install %s pointing at %s\n' \
      "$unit_path" "$compose_file"
    return 0
  fi

  if [[ -f "$unit_path" ]] && printf '%s' "$unit_content" | "${ROOT_CMD[@]}" cmp -s - "$unit_path" 2>/dev/null; then
    log "marvis-stack.service already up to date"
  else
    log "Installing marvis-stack.service"
    printf '%s' "$unit_content" | "${ROOT_CMD[@]}" tee "$unit_path" >/dev/null
    run_root systemctl daemon-reload
  fi

  run_root systemctl enable marvis-stack.service
  log "marvis-stack.service enabled; will run at next boot. To start now: systemctl start marvis-stack"
}
