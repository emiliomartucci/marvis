# shellcheck shell=bash
# core/scripts/lib/setup-server/proxy-cloudflared.sh
#
# Cloudflare Tunnel (cloudflared) reverse proxy path.
# Activated when MARVIS_PROXY_MODE=cloudflare (default if CF token is set).
#
# Sensitive: MARVIS_CLOUDFLARE_TUNNEL_TOKEN is masked in dry-run output via
# run_root_sensitive().

ensure_cloudflared_repo_apt() {
  local keyring="/usr/share/keyrings/cloudflare-main.gpg"
  local list_file="/etc/apt/sources.list.d/cloudflared.list"
  local repo_line="deb [signed-by=${keyring}] https://pkg.cloudflare.com/cloudflared any main"

  run_root install -d -m 0755 /usr/share/keyrings

  if root_status test -f "$keyring"; then
    log "Cloudflare apt keyring already exists"
  else
    log "Installing Cloudflare apt keyring"
    run_root bash -c "curl -fsSL 'https://pkg.cloudflare.com/cloudflare-main.gpg' | gpg --dearmor -o '${keyring}'"
    run_root chmod a+r "$keyring"
  fi

  if root_status grep -qxF -- "$repo_line" "$list_file" 2>/dev/null; then
    log "Cloudflare apt repository already configured"
  else
    log "Configuring Cloudflare apt repository"
    run_root bash -c "printf '%s\n' '${repo_line}' > '${list_file}'"
  fi
}

ensure_cloudflared_repo_dnf() {
  local repo_file="/etc/yum.repos.d/cloudflared-ascii.repo"
  local repo_url="https://pkg.cloudflare.com/cloudflared-ascii.repo"

  if root_status test -f "$repo_file"; then
    log "Cloudflare dnf repository already configured"
    return 0
  fi

  log "Configuring Cloudflare dnf repository"
  run_root bash -c "curl -fsSL '${repo_url}' -o '${repo_file}'"
}

ensure_cloudflared() {
  mark_step "proxy-cloudflared"
  if [[ -z "$MARVIS_CLOUDFLARE_TUNNEL_TOKEN" ]]; then
    log "Cloudflare Tunnel token not set; skipping cloudflared install."
    return 0
  fi

  if ! command_exists cloudflared; then
    case "${PKG_MANAGER:-apt}" in
      apt)
        ensure_cloudflared_repo_apt
        apt_update
        install_packages_apt cloudflared
        ;;
      dnf)
        ensure_cloudflared_repo_dnf
        install_packages_dnf cloudflared
        ;;
      *)
        fail "Cannot install cloudflared: unknown PKG_MANAGER=${PKG_MANAGER}"
        ;;
    esac
  else
    log "cloudflared already installed"
  fi

  if ! command_exists systemctl; then
    warn "systemctl not available; cloudflared installed but service setup skipped."
    return 0
  fi

  if systemd_unit_exists cloudflared.service; then
    log "cloudflared service already exists; leaving existing tunnel token unchanged."
  else
    log "Installing cloudflared service from token"
    run_root_sensitive "cloudflared service install <redacted-token>" \
      cloudflared service install "$MARVIS_CLOUDFLARE_TUNNEL_TOKEN"
  fi

  run_root systemctl enable --now cloudflared
}
