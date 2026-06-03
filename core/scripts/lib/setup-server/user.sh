# shellcheck shell=bash
# core/scripts/lib/setup-server/user.sh
#
# Deploy user lifecycle:
#   - useradd (idempotent via id -u check)
#   - base directory ownership
#   - optional passwordless sudo (NOPASSWD sudoers drop-in)
#   - authorized_keys with MARVIS_SSH_PUBLIC_KEY

ensure_deploy_user() {
  mark_step "user"
  if id "$MARVIS_DEPLOY_USER" >/dev/null 2>&1; then
    log "Deploy user already exists: $MARVIS_DEPLOY_USER"
  else
    log "Creating deploy user: $MARVIS_DEPLOY_USER"
    run_root useradd --create-home --shell /bin/bash "$MARVIS_DEPLOY_USER"
  fi

  log "Ensuring base directory ownership: $MARVIS_BASE_DIR"
  run_root install -d -m 0755 -o "$MARVIS_DEPLOY_USER" -g "$MARVIS_DEPLOY_USER" "$MARVIS_BASE_DIR"

  if [[ "$MARVIS_ENABLE_PASSWORDLESS_SUDO" == "1" ]]; then
    local sudoers_file="/etc/sudoers.d/90-marvis-${MARVIS_DEPLOY_USER}"
    log "Enabling passwordless sudo for $MARVIS_DEPLOY_USER"
    # Wheel on dnf-family, sudo on apt-family. usermod -aG is idempotent.
    if [[ "${PKG_MANAGER:-apt}" == "dnf" ]]; then
      run_root usermod -aG wheel "$MARVIS_DEPLOY_USER"
    else
      run_root usermod -aG sudo "$MARVIS_DEPLOY_USER"
    fi
    if is_dry_run; then
      print_command bash -c "printf '%s\n' '${MARVIS_DEPLOY_USER} ALL=(ALL) NOPASSWD:ALL' > '$sudoers_file'"
    else
      printf '%s ALL=(ALL) NOPASSWD:ALL\n' "$MARVIS_DEPLOY_USER" | "${ROOT_CMD[@]}" tee "$sudoers_file" >/dev/null
      "${ROOT_CMD[@]}" chmod 0440 "$sudoers_file"
      "${ROOT_CMD[@]}" visudo -cf "$sudoers_file" >/dev/null
    fi
  else
    log "Passwordless sudo disabled. Set MARVIS_ENABLE_PASSWORDLESS_SUDO=1 to enable it."
  fi
}

ensure_ssh_key() {
  mark_step "ssh-key"
  local home_dir
  home_dir="$(getent passwd "$MARVIS_DEPLOY_USER" | cut -d: -f6 || true)"
  if [[ -z "$home_dir" ]]; then
    if is_dry_run; then
      home_dir="/home/$MARVIS_DEPLOY_USER"
    else
      fail "Cannot resolve home directory for $MARVIS_DEPLOY_USER."
    fi
  fi

  local ssh_dir="$home_dir/.ssh"
  local authorized_keys="$ssh_dir/authorized_keys"

  run_root install -d -m 0700 -o "$MARVIS_DEPLOY_USER" -g "$MARVIS_DEPLOY_USER" "$ssh_dir"
  run_root touch "$authorized_keys"
  run_root chown "$MARVIS_DEPLOY_USER:$MARVIS_DEPLOY_USER" "$authorized_keys"
  run_root chmod 0600 "$authorized_keys"

  if [[ -z "$MARVIS_SSH_PUBLIC_KEY" ]]; then
    warn "MARVIS_SSH_PUBLIC_KEY is empty; paste your public key into $authorized_keys later."
    return 0
  fi

  if root_status grep -qxF -- "$MARVIS_SSH_PUBLIC_KEY" "$authorized_keys"; then
    log "SSH public key already present for $MARVIS_DEPLOY_USER"
  else
    log "Adding SSH public key for $MARVIS_DEPLOY_USER"
    run_root bash -c 'printf "%s\n" "$1" >> "$2"' bash "$MARVIS_SSH_PUBLIC_KEY" "$authorized_keys"
  fi
}
