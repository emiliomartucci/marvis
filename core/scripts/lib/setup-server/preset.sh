# shellcheck shell=bash
# core/scripts/lib/setup-server/preset.sh
#
# Applies a coherent bundle of MARVIS_* env vars based on MARVIS_PRESET.
# Presets are non-destructive: they only set vars that are still unset (i.e. the
# user can override any field by exporting it before running the script).
#
# Supported presets:
#   - hetzner-cax41  Marvisx prod (CF Tunnel, UFW on, /opt/marvis, tmux on)
#   - oss-default    OSS clone-to-boot (Caddy+LE, UFW on, tmux off, user marvis)
#   - dev-local      Laptop / firewall-interno (selfsigned, no UFW, no SSH key)

set_if_unset() {
  local name="$1"
  local value="$2"
  local current="${!name-}"
  if [[ -z "$current" ]]; then
    printf -v "$name" '%s' "$value"
    export "${name?}"
  fi
}

apply_preset() {
  local preset="${MARVIS_PRESET:-}"
  if [[ -z "$preset" ]]; then
    return 0
  fi

  case "$preset" in
    hetzner-cax41)
      set_if_unset MARVIS_PROXY_MODE "cloudflare"
      set_if_unset MARVIS_DEPLOY_USER "openclaw"
      set_if_unset MARVIS_BASE_DIR "/opt/marvis"
      set_if_unset MARVIS_ENABLE_UFW "1"
      set_if_unset MARVIS_START_TMUX "1"
      set_if_unset MARVIS_ENABLE_SYSTEMD_STACK "0"
      ;;
    oss-default)
      set_if_unset MARVIS_PROXY_MODE "caddy"
      set_if_unset MARVIS_DEPLOY_USER "marvis"
      set_if_unset MARVIS_BASE_DIR "/opt/marvis"
      set_if_unset MARVIS_ENABLE_UFW "1"
      set_if_unset MARVIS_START_TMUX "0"
      set_if_unset MARVIS_ENABLE_SYSTEMD_STACK "1"
      ;;
    dev-local)
      set_if_unset MARVIS_PROXY_MODE "selfsigned"
      set_if_unset MARVIS_DOMAIN "marvis.local"
      set_if_unset MARVIS_DEPLOY_USER "${USER:-$(id -un)}"
      set_if_unset MARVIS_BASE_DIR "$HOME/marvis-dev"
      set_if_unset MARVIS_ENABLE_UFW "0"
      set_if_unset MARVIS_START_TMUX "0"
      set_if_unset MARVIS_ENABLE_SYSTEMD_STACK "0"
      ;;
    *)
      fail "Unknown MARVIS_PRESET='$preset'. Supported: hetzner-cax41 | oss-default | dev-local."
      ;;
  esac

  log "Applied preset: $preset"
}
