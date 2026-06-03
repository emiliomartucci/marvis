# shellcheck shell=bash
# core/scripts/lib/setup-server/tmux.sh
#
# Persistent tmux session for the deploy user (preserves Hetzner Marvisx prod
# UX where ops sessions survive across SSH disconnect).
#
# Opt-out via MARVIS_START_TMUX=0.

ensure_tmux() {
  mark_step "tmux"
  if [[ "$MARVIS_START_TMUX" != "1" ]]; then
    log "tmux startup disabled by MARVIS_START_TMUX=0"
    return 0
  fi

  local socket_path="$MARVIS_TMUX_DIR/marvis.sock"

  log "Ensuring persistent tmux socket directory: $MARVIS_TMUX_DIR"
  run_root install -d -m 0700 -o "$MARVIS_DEPLOY_USER" -g "$MARVIS_DEPLOY_USER" "$MARVIS_TMUX_DIR"

  if deploy_user_status env TMUX_TMPDIR="$MARVIS_TMUX_DIR" tmux -S "$socket_path" has-session -t marvis 2>/dev/null; then
    log "tmux session already running: marvis"
  else
    log "Starting tmux session: marvis"
    run_as_deploy_user env TMUX_TMPDIR="$MARVIS_TMUX_DIR" tmux -S "$socket_path" new-session -d -s marvis
  fi
}
