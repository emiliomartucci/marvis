# shellcheck shell=bash
# core/scripts/lib/setup-server/docker.sh
#
# Docker CE + compose plugin install:
#   - apt path: download.docker.com signed apt repo for $DOCKER_OS_ID/$OS_CODENAME
#   - dnf path: download.docker.com dnf repo for Fedora (or CentOS for RHEL-family)
#
# Pre-flight: skip install entirely if `docker compose version` already works.

ensure_docker_repo_apt() {
  local keyring="/etc/apt/keyrings/docker.gpg"
  local list_file="/etc/apt/sources.list.d/docker.list"
  local repo_line
  local arch

  arch="$(dpkg --print-architecture)"
  repo_line="deb [arch=${arch} signed-by=${keyring}] https://download.docker.com/linux/${DOCKER_OS_ID} ${OS_CODENAME} stable"

  run_root install -d -m 0755 /etc/apt/keyrings

  if root_status test -f "$keyring"; then
    log "Docker apt keyring already exists"
  else
    log "Installing Docker apt keyring"
    run_root bash -c "curl -fsSL 'https://download.docker.com/linux/${DOCKER_OS_ID}/gpg' | gpg --dearmor -o '${keyring}'"
    run_root chmod a+r "$keyring"
  fi

  if root_status grep -qxF -- "$repo_line" "$list_file" 2>/dev/null; then
    log "Docker apt repository already configured"
  else
    log "Configuring Docker apt repository"
    run_root bash -c "printf '%s\n' '${repo_line}' > '${list_file}'"
  fi
}

ensure_docker_repo_dnf() {
  local repo_file="/etc/yum.repos.d/docker-ce.repo"
  local repo_url="https://download.docker.com/linux/${DOCKER_OS_ID}/docker-ce.repo"

  if root_status test -f "$repo_file"; then
    log "Docker dnf repository already configured: $repo_file"
    return 0
  fi

  log "Configuring Docker dnf repository for $DOCKER_OS_ID"
  # dnf config-manager is the canonical way; dnf-plugins-core ships it on Fedora.
  if command_exists dnf-3 || command_exists dnf; then
    # On Fedora 41+ the plugin is bundled as dnf5-plugins; on F39/F40 we may need
    # dnf-plugins-core. Pre-install it idempotently.
    install_packages_dnf dnf-plugins-core
  fi
  run_root dnf config-manager --add-repo "$repo_url"
}

ensure_docker() {
  mark_step "docker"
  if command_exists docker && docker compose version >/dev/null 2>&1; then
    log "Docker and compose plugin already available"
  else
    case "${PKG_MANAGER:-apt}" in
      apt)
        ensure_docker_repo_apt
        apt_update
        install_packages_apt \
          docker-ce \
          docker-ce-cli \
          containerd.io \
          docker-buildx-plugin \
          docker-compose-plugin
        ;;
      dnf)
        ensure_docker_repo_dnf
        install_packages_dnf \
          docker-ce \
          docker-ce-cli \
          containerd.io \
          docker-buildx-plugin \
          docker-compose-plugin
        ;;
      *)
        fail "Cannot install Docker: unknown PKG_MANAGER=${PKG_MANAGER}"
        ;;
    esac
  fi

  if command_exists systemctl; then
    log "Enabling Docker service"
    run_root systemctl enable --now docker
  else
    warn "systemctl not available; skipping Docker service enable."
  fi

  if getent group docker >/dev/null 2>&1 || is_dry_run; then
    if ! is_dry_run && id -nG "$MARVIS_DEPLOY_USER" | tr ' ' '\n' | grep -qx docker; then
      log "Deploy user already belongs to docker group"
    else
      log "Adding deploy user to docker group"
      run_root usermod -aG docker "$MARVIS_DEPLOY_USER"
      warn "Docker group changes require a new login session for $MARVIS_DEPLOY_USER."
    fi
  else
    warn "docker group not found after install; skipping group membership."
  fi
}
