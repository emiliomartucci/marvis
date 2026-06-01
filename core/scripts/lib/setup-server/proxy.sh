# shellcheck shell=bash
# core/scripts/lib/setup-server/proxy.sh
#
# Reverse-proxy dispatcher. Reads MARVIS_PROXY_MODE and calls the matching
# proxy-*.sh helper. Default resolution:
#   - cloudflare  if MARVIS_CLOUDFLARE_TUNNEL_TOKEN is set
#   - caddy       if MARVIS_DOMAIN is set but no CF token (OSS default)
#   - none        otherwise

resolve_proxy_mode() {
  if [[ -n "${MARVIS_PROXY_MODE:-}" ]]; then
    return 0
  fi

  if [[ -n "$MARVIS_CLOUDFLARE_TUNNEL_TOKEN" ]]; then
    MARVIS_PROXY_MODE="cloudflare"
  elif [[ -n "$MARVIS_DOMAIN" ]]; then
    MARVIS_PROXY_MODE="caddy"
  else
    MARVIS_PROXY_MODE="none"
  fi
  export MARVIS_PROXY_MODE
}

ensure_proxy() {
  resolve_proxy_mode
  log "Resolved proxy mode: $MARVIS_PROXY_MODE"

  case "$MARVIS_PROXY_MODE" in
    cloudflare)
      ensure_cloudflared
      ;;
    caddy)
      ensure_caddy
      ;;
    selfsigned)
      ensure_selfsigned
      ;;
    none)
      mark_step "proxy-none"
      log "Proxy mode 'none'; no reverse proxy installed. Stack will bind to localhost."
      ;;
    *)
      fail "Unknown MARVIS_PROXY_MODE='$MARVIS_PROXY_MODE'. Allowed: cloudflare|caddy|selfsigned|none."
      ;;
  esac
}
