#!/usr/bin/env bash
# Thin wrapper around core/scripts/safety_bridge.py

set -euo pipefail

HOOK_DIR="$(cd "$(dirname "$0")" && pwd)"
if [ ! -r "$HOOK_DIR/_config.sh" ]; then
  printf '%s\n' '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"Hook dependency missing: _config.sh; blocked fail-closed."}}'
  exit 0
fi
source "$HOOK_DIR/_config.sh"

INPUT=$(cat)
if [ -f "$HOOK_DIR/safety_bridge.py" ]; then
  # Self-contained install: `marvis hooks install` ships safety_bridge.py here.
  BRIDGE="$HOOK_DIR/safety_bridge.py"
else
  # Source-checkout fallback: hooks live in .claude/hooks, bridge in core/scripts.
  BRIDGE="$(cd "$HOOK_DIR/../.." && pwd)/core/scripts/safety_bridge.py"
fi

if ! OUTPUT=$(printf '%s' "$INPUT" | python3 "$BRIDGE" hook --rule secret-scan); then
  deny "Safety bridge failure (secret-scan) - blocked fail-closed."
fi

[ -n "$OUTPUT" ] && printf '%s\n' "$OUTPUT"
exit 0
