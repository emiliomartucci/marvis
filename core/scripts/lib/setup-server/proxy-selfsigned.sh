# shellcheck shell=bash
# core/scripts/lib/setup-server/proxy-selfsigned.sh
#
# Self-signed TLS reverse proxy path for dev-local / firewall-interno setups.
# Activated when MARVIS_PROXY_MODE=selfsigned.
#
# Generates a 10-year self-signed cert for $MARVIS_DOMAIN (default: marvis.local)
# in /etc/ssl/marvis/, then drops a Caddyfile that pins the local cert.

ensure_selfsigned() {
  mark_step "proxy-selfsigned"

  local domain="${MARVIS_DOMAIN:-marvis.local}"
  local cert_dir="/etc/ssl/marvis"
  local cert_path="$cert_dir/cert.pem"
  local key_path="$cert_dir/key.pem"

  if ! command_exists openssl; then
    case "${PKG_MANAGER:-apt}" in
      apt) install_packages_apt openssl ;;
      dnf) install_packages_dnf openssl ;;
      *) fail "Cannot install openssl: unknown PKG_MANAGER=${PKG_MANAGER}" ;;
    esac
  fi

  if ! command_exists caddy; then
    # Reuse caddy install path for dev-local.
    case "${PKG_MANAGER:-apt}" in
      apt)
        ensure_caddy_repo_apt
        apt_update
        install_packages_apt caddy
        ;;
      dnf)
        install_packages_dnf caddy
        ;;
    esac
  fi

  run_root install -d -m 0755 "$cert_dir"

  if root_status test -f "$cert_path" && root_status test -f "$key_path"; then
    log "Self-signed cert already present at $cert_path"
  else
    log "Generating self-signed cert for $domain (10y, RSA 2048)"
    run_root openssl req -x509 -nodes \
      -days 3650 \
      -newkey rsa:2048 \
      -keyout "$key_path" \
      -out "$cert_path" \
      -subj "/CN=${domain}"
    run_root chmod 0600 "$key_path"
    run_root chmod 0644 "$cert_path"
  fi

  local caddyfile="/etc/caddy/Caddyfile"
  run_root install -d -m 0755 /etc/caddy

  local rendered
  rendered="$(cat <<EOF
${domain} {
\ttls ${cert_path} ${key_path}
\treverse_proxy /api/* localhost:8100 {
\t\theader_up -CF-Connecting-IP
\t\theader_up X-Marvis-Client-IP {http.request.remote.host}
\t}
\treverse_proxy localhost:3000
}
EOF
)"

  if is_dry_run; then
    printf '[setup-server] dry-run: would write self-signed Caddyfile for %s to %s\n' \
      "$domain" "$caddyfile"
  elif [[ -f "$caddyfile" ]] && printf '%s' "$rendered" | "${ROOT_CMD[@]}" cmp -s - "$caddyfile" 2>/dev/null; then
    log "Self-signed Caddyfile already up to date"
  else
    log "Writing self-signed Caddyfile to $caddyfile"
    printf '%s' "$rendered" | "${ROOT_CMD[@]}" tee "$caddyfile" >/dev/null
  fi

  if command_exists systemctl; then
    run_root systemctl enable --now caddy
  fi
}
