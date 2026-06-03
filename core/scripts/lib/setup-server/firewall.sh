# shellcheck shell=bash
# core/scripts/lib/setup-server/firewall.sh
#
# Firewall configuration:
#   - apt hosts: UFW (22/80/443)
#   - dnf hosts: firewalld (ssh + http + https)
#
# Opt-out via MARVIS_ENABLE_UFW=0 (the env var name is preserved for backward
# compat; it means "enable host firewall" regardless of backend).

ensure_firewall() {
  mark_step "firewall"
  if [[ "$MARVIS_ENABLE_UFW" != "1" ]]; then
    log "Host firewall disabled by MARVIS_ENABLE_UFW=0"
    return 0
  fi

  case "${PKG_MANAGER:-apt}" in
    apt)
      ensure_firewall_ufw
      ;;
    dnf)
      ensure_firewall_firewalld
      ;;
    *)
      warn "Unknown PKG_MANAGER=${PKG_MANAGER}; skipping firewall config."
      ;;
  esac
}

ensure_firewall_ufw() {
  log "Configuring UFW for SSH, HTTP, and HTTPS"
  run_root ufw allow 22/tcp
  run_root ufw allow 80/tcp
  run_root ufw allow 443/tcp
  run_root ufw --force enable
}

ensure_firewall_firewalld() {
  log "Configuring firewalld for SSH, HTTP, and HTTPS"
  # firewalld systemd unit must be running before firewall-cmd accepts permanent edits.
  if command_exists systemctl; then
    run_root systemctl enable --now firewalld
  fi
  run_root firewall-cmd --permanent --add-service=ssh
  run_root firewall-cmd --permanent --add-service=http
  run_root firewall-cmd --permanent --add-service=https
  run_root firewall-cmd --reload
}
