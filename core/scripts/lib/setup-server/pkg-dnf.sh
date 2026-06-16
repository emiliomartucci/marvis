# shellcheck shell=bash
# core/scripts/lib/setup-server/pkg-dnf.sh
#
# Fedora/RHEL-family (dnf) package manager helpers.
# Sourced only when PKG_MANAGER=dnf.
#
# Idempotency: dnf install -y is naturally idempotent (returns 0 if already installed)
# but we still pre-check to avoid useless dnf invocations + keep log output coherent
# with the apt path.

package_installed_dnf() {
  rpm -q "$1" >/dev/null 2>&1
}

install_packages_dnf() {
  local missing=()
  local pkg

  for pkg in "$@"; do
    if package_installed_dnf "$pkg"; then
      log "Package already installed: $pkg"
    else
      missing+=("$pkg")
    fi
  done

  if [[ ${#missing[@]} -eq 0 ]]; then
    return 0
  fi

  log "Installing packages: ${missing[*]}"
  run_root dnf install -y "${missing[@]}"
}

ensure_base_packages_dnf() {
  mark_step "pkg-dnf"
  install_packages_dnf \
    ca-certificates \
    curl \
    gnupg2 \
    jq \
    python3 \
    python3-pip \
    sudo \
    nodejs \
    npm \
    git \
    tmux \
    firewalld
}
