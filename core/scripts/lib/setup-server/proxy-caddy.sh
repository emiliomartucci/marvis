# shellcheck shell=bash
# core/scripts/lib/setup-server/proxy-caddy.sh
#
# Caddy + Let's Encrypt reverse proxy path (OSS default).
# Activated when MARVIS_PROXY_MODE=caddy.
#
# Single source of truth for the Caddyfile: deploy/_template/caddy/Caddyfile.example
# (from P0.6-A scaffold). The template now uses native Caddy env-var syntax
# ({$PUBLIC_DOMAIN}, {$ACME_EMAIL}), so this module copies it verbatim and
# materializes the env file the systemd unit picks up.
#
# Pre-flight: requires DNS A/AAAA records for $MARVIS_DOMAIN already resolving
# to this host (Let's Encrypt http-01 challenge). The script warns but does not
# block — the operator may complete DNS before the first request.

ensure_caddy_repo_apt() {
  local keyring="/usr/share/keyrings/caddy-stable-archive-keyring.gpg"
  local list_file="/etc/apt/sources.list.d/caddy-stable.list"
  local repo_line="deb [signed-by=${keyring}] https://dl.cloudsmith.io/public/caddy/stable/deb/debian any-version main"

  run_root install -d -m 0755 /usr/share/keyrings

  if root_status test -f "$keyring"; then
    log "Caddy apt keyring already exists"
  else
    log "Installing Caddy apt keyring"
    run_root bash -c "curl -fsSL 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor -o '${keyring}'"
    run_root chmod a+r "$keyring"
  fi

  if root_status grep -qxF -- "$repo_line" "$list_file" 2>/dev/null; then
    log "Caddy apt repository already configured"
  else
    log "Configuring Caddy apt repository"
    run_root bash -c "printf '%s\n' '${repo_line}' > '${list_file}'"
  fi
}

ensure_caddy_repo_dnf() {
  # Fedora ships caddy in the default repos starting F38. Prefer that path; the
  # COPR fallback exists for older RHEL-family hosts but is out of scope here.
  log "Using distro caddy package on dnf hosts (F39+ ships Caddy 2.x)"
}

render_caddyfile() {
  local template_path="$1"
  local target_path="$2"

  if [[ ! -r "$template_path" ]]; then
    fail "Caddyfile template not found at $template_path. Did P0.6-A scaffold run?"
  fi

  local rendered
  rendered="$(cat "$template_path")"

  if is_dry_run; then
    printf '[setup-server] dry-run: would copy Caddyfile to %s (from %s)\n' \
      "$target_path" "$template_path"
    return 0
  fi

  # Hash-compare before overwrite to avoid unnecessary systemd reload.
  if [[ -f "$target_path" ]] && \
       printf '%s' "$rendered" | "${ROOT_CMD[@]}" cmp -s - "$target_path" 2>/dev/null; then
    log "Caddyfile already up to date at $target_path"
    return 0
  fi

  log "Writing Caddyfile to $target_path"
  printf '%s' "$rendered" | "${ROOT_CMD[@]}" tee "$target_path" >/dev/null
}

write_caddy_env_defaults() {
  local env_file="/etc/default/caddy"
  local rendered
  rendered="$(cat <<EOF
# Written by core/scripts/lib/setup-server/proxy-caddy.sh.
# Consumed by the caddy.service systemd unit (EnvironmentFile=-/etc/default/caddy).
# Manual edits are overwritten on the next setup-server run.
PUBLIC_DOMAIN=${MARVIS_DOMAIN}
ACME_EMAIL=${MARVIS_LETSENCRYPT_EMAIL}
MARVIS_API_UPSTREAM=${MARVIS_CADDY_API_UPSTREAM:-127.0.0.1:8100}
MARVIS_CONSOLE_UPSTREAM=${MARVIS_CADDY_CONSOLE_UPSTREAM:-127.0.0.1:3000}
EOF
)"

  if is_dry_run; then
    printf '[setup-server] dry-run: would write caddy env defaults to %s\n' "$env_file"
    return 0
  fi

  if [[ -f "$env_file" ]] && \
       printf '%s' "$rendered" | "${ROOT_CMD[@]}" cmp -s - "$env_file" 2>/dev/null; then
    log "Caddy env defaults already up to date at $env_file"
    return 0
  fi

  log "Writing Caddy env defaults to $env_file"
  printf '%s' "$rendered" | "${ROOT_CMD[@]}" tee "$env_file" >/dev/null
}

ensure_caddy() {
  mark_step "proxy-caddy"

  if [[ -z "$MARVIS_DOMAIN" ]]; then
    fail "MARVIS_PROXY_MODE=caddy requires MARVIS_DOMAIN to be set."
  fi
  if [[ -z "$MARVIS_LETSENCRYPT_EMAIL" ]]; then
    warn "MARVIS_LETSENCRYPT_EMAIL not set; falling back to MARVIS_EMAIL=$MARVIS_EMAIL."
    MARVIS_LETSENCRYPT_EMAIL="$MARVIS_EMAIL"
  fi
  if [[ -z "$MARVIS_LETSENCRYPT_EMAIL" ]]; then
    fail "MARVIS_PROXY_MODE=caddy requires MARVIS_LETSENCRYPT_EMAIL (or MARVIS_EMAIL) for Let's Encrypt."
  fi

  if ! command_exists caddy; then
    case "${PKG_MANAGER:-apt}" in
      apt)
        ensure_caddy_repo_apt
        apt_update
        install_packages_apt caddy
        ;;
      dnf)
        ensure_caddy_repo_dnf
        install_packages_dnf caddy
        ;;
      *)
        fail "Cannot install caddy: unknown PKG_MANAGER=${PKG_MANAGER}"
        ;;
    esac
  else
    log "caddy already installed"
  fi

  run_root install -d -m 0755 /etc/caddy
  render_caddyfile \
    "${MARVIS_CADDY_TEMPLATE:-${MARVIS_REPO_ROOT}/deploy/_template/caddy/Caddyfile.example}" \
    /etc/caddy/Caddyfile
  write_caddy_env_defaults

  if ! command_exists systemctl; then
    warn "systemctl not available; caddy installed but service setup skipped."
    return 0
  fi

  log "Enabling caddy service (Let's Encrypt cert issued on first request)"
  run_root systemctl enable --now caddy
  warn "Make sure DNS A/AAAA for $MARVIS_DOMAIN points to this host before traffic hits Caddy."
}
