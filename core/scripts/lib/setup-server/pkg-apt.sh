# shellcheck shell=bash
# core/scripts/lib/setup-server/pkg-apt.sh
#
# Debian/Ubuntu (apt) package manager helpers.
# Sourced only when PKG_MANAGER=apt.

package_installed_apt() {
  dpkg-query -W -f='${Status}' "$1" 2>/dev/null | grep -q 'install ok installed'
}

apt_update() {
  log "Updating apt package index"
  run_root apt-get update
}

install_packages_apt() {
  local missing=()
  local pkg

  for pkg in "$@"; do
    if package_installed_apt "$pkg"; then
      log "Package already installed: $pkg"
    else
      missing+=("$pkg")
    fi
  done

  if [[ ${#missing[@]} -eq 0 ]]; then
    return 0
  fi

  log "Installing packages: ${missing[*]}"
  run_root env DEBIAN_FRONTEND=noninteractive apt-get install -y "${missing[@]}"
}

ensure_base_packages_apt() {
  mark_step "pkg-apt"
  apt_update
  install_packages_apt \
    ca-certificates \
    curl \
    gnupg \
    jq \
    lsb-release \
    python3 \
    python3-pip \
    sudo \
    nodejs \
    npm \
    git \
    tmux \
    ufw
}
