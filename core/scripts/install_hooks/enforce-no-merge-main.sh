#!/usr/bin/env bash
# Thin wrapper around core/scripts/safety_bridge.py

set -euo pipefail

source "$(dirname "$0")/_config.sh"

INPUT=$(cat)
HOOK_DIR="$(cd "$(dirname "$0")" && pwd)"
if [ -f "$HOOK_DIR/safety_bridge.py" ]; then
  # Self-contained install: `marvis hooks install` ships safety_bridge.py here.
  BRIDGE="$HOOK_DIR/safety_bridge.py"
else
  # Source-checkout fallback: hooks live in .claude/hooks, bridge in core/scripts.
  BRIDGE="$(cd "$HOOK_DIR/../.." && pwd)/core/scripts/safety_bridge.py"
fi

if ! OUTPUT=$(printf '%s' "$INPUT" | python3 "$BRIDGE" hook --rule bash-merge); then
  deny "Safety bridge failure (bash-merge) - blocked fail-closed."
fi

[ -n "$OUTPUT" ] && printf '%s\n' "$OUTPUT"
exit 0
