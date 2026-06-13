# shellcheck shell=bash
# core/scripts/lib/setup-server/os-detect.sh
#
# Detects host OS family and exports:
#   OS_ID            (ubuntu|debian|fedora|rhel|rocky|almalinux)
#   OS_ID_LIKE       (raw ID_LIKE from /etc/os-release)
#   OS_CODENAME      (Debian-family codename, empty on dnf hosts)
#   OS_VERSION_ID    (e.g. 22.04, 12, 39)
#   PKG_MANAGER      (apt|dnf)
#   DOCKER_OS_ID     (ubuntu|debian|fedora) — used by Docker apt/dnf repo URL
#
# Supports Ubuntu 22+/Debian 12+/Fedora 39+ (RHEL-family via ID_LIKE fallback).

load_os_release() {
  [[ -r /etc/os-release ]] || fail "/etc/os-release not found."
  # shellcheck disable=SC1091
  . /etc/os-release

  OS_ID="${ID:-}"
  OS_ID_LIKE="${ID_LIKE:-}"
  OS_CODENAME="${VERSION_CODENAME:-}"
  OS_VERSION_ID="${VERSION_ID:-}"

  case "$OS_ID" in
    ubuntu|debian)
      PKG_MANAGER="apt"
      DOCKER_OS_ID="$OS_ID"
      [[ -n "$OS_CODENAME" ]] || fail "VERSION_CODENAME missing in /etc/os-release."
      ;;
    fedora)
      PKG_MANAGER="dnf"
      DOCKER_OS_ID="fedora"
      ;;
    rhel|rocky|almalinux|centos)
      PKG_MANAGER="dnf"
      # Docker upstream repo for RHEL-family targets the "centos" path historically.
      DOCKER_OS_ID="centos"
      ;;
    *)
      case "$OS_ID_LIKE" in
        *ubuntu*)
          PKG_MANAGER="apt"
          DOCKER_OS_ID="ubuntu"
          [[ -n "$OS_CODENAME" ]] || fail "VERSION_CODENAME missing in /etc/os-release."
          ;;
        *debian*)
          PKG_MANAGER="apt"
          DOCKER_OS_ID="debian"
          [[ -n "$OS_CODENAME" ]] || fail "VERSION_CODENAME missing in /etc/os-release."
          ;;
        *fedora*|*rhel*)
          PKG_MANAGER="dnf"
          DOCKER_OS_ID="fedora"
          ;;
        *)
          fail "Unsupported OS '$OS_ID' (ID_LIKE='$OS_ID_LIKE'). Supported: Ubuntu 22+, Debian 12+, Fedora 39+."
          ;;
      esac
      ;;
  esac

  export OS_ID OS_ID_LIKE OS_CODENAME OS_VERSION_ID PKG_MANAGER DOCKER_OS_ID
}

ensure_os_supported() {
  mark_step "os-detect"
  load_os_release
  log "Detected os=$OS_ID version=$OS_VERSION_ID codename=${OS_CODENAME:-n/a} pkg=$PKG_MANAGER"
}
