#!/usr/bin/env bash
# v1.0.0 - 2026-03-12 - Shared helpers for centralized hook configuration
# Source this from any hook: source "$(dirname "$0")/_config.sh"

# --- Config Loading ---
# Fail-CLOSED: if config.json is missing or unparseable, block everything.

_HOOK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
_CONFIG_FILE="$_HOOK_DIR/config.json"

cfg_load() {
  if [ ! -f "$_CONFIG_FILE" ]; then
    echo "FATAL: Hook config missing: $_CONFIG_FILE" >&2
    return 1
  fi
  # Validate JSON
  if ! jq empty "$_CONFIG_FILE" 2>/dev/null; then
    echo "FATAL: Hook config invalid JSON: $_CONFIG_FILE" >&2
    return 1
  fi
  return 0
}

cfg_get() {
  # Usage: cfg_get '.branches.task_pattern'
  jq -r "$1 // empty" "$_CONFIG_FILE" 2>/dev/null
}

cfg_array() {
  # Usage: cfg_array '.worktree.blocked_files' → one item per line
  jq -r "$1[]? // empty" "$_CONFIG_FILE" 2>/dev/null
}

cfg_contains() {
  # Usage: cfg_contains '.worktree.blocked_files' "settings.json"
  local path="$1" value="$2"
  jq -e --arg v "$value" "$path | index(\$v) != null" "$_CONFIG_FILE" >/dev/null 2>&1
}

# --- Branch Helpers ---

is_protected_branch() {
  local branch="$1"
  cfg_contains '.branches.protected' "$branch"
}

matches_task_branch() {
  local branch="$1"
  local pattern
  pattern=$(cfg_get '.branches.task_pattern')
  [ -n "$pattern" ] && echo "$branch" | grep -qE "$pattern"
}

# --- Worktree Whitelist ---

is_whitelisted_path() {
  # Check if a relative path is in the worktree whitelist.
  # Returns 0 (allowed) or 1 (blocked).
  local rel_path="$1"

  # Check blocked files FIRST (takes priority over whitelist)
  while IFS= read -r blocked; do
    [ -z "$blocked" ] && continue
    [ "$rel_path" = "$blocked" ] && return 1
  done < <(cfg_array '.worktree.blocked_files')

  # Check extension whitelist
  local ext="${rel_path##*.}"
  while IFS= read -r wext; do
    [ -z "$wext" ] && continue
    wext="${wext#.}"  # strip leading dot
    [ "$ext" = "$wext" ] && return 0
  done < <(cfg_array '.worktree.whitelist_extensions')

  # Check directory whitelist (prefix match)
  while IFS= read -r wdir; do
    [ -z "$wdir" ] && continue
    case "$rel_path" in ${wdir}*) return 0 ;; esac
  done < <(cfg_array '.worktree.whitelist_dirs')

  # Check exact file whitelist
  while IFS= read -r wfile; do
    [ -z "$wfile" ] && continue
    [ "$rel_path" = "$wfile" ] && return 0
  done < <(cfg_array '.worktree.whitelist_files')

  # Check script patterns
  while IFS= read -r wpat; do
    [ -z "$wpat" ] && continue
    # shellcheck disable=SC2254
    case "$rel_path" in $wpat) return 0 ;; esac
  done < <(cfg_array '.worktree.whitelist_scripts')

  return 1  # Not whitelisted
}

# --- Deny Helper (DRY) ---

deny() {
  local reason="$1"
  jq -n --arg r "$reason" '{
    hookSpecificOutput: {
      hookEventName: "PreToolUse",
      permissionDecision: "deny",
      permissionDecisionReason: $r
    }
  }'
  exit 0
}
