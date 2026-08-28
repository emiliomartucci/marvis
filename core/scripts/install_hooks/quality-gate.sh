#!/usr/bin/env bash
# v1.3.0 - 2026-04-22 - Phase 7.x hygiene: CWD-aware + consolidated git diff + fail-closed
# v1.2.0 - 2026-04-22 - Phase 7.2: add MCP smoke test (prevents Zod crash regression, learning f2663d51)
# v1.1.0 - 2026-04-17 - Wire migration version check (migration 082/083 P1)
# Thin wrapper around core/scripts/safety_bridge.py + migration-version gate

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
REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"

if ! OUTPUT=$(printf '%s' "$INPUT" | python3 "$BRIDGE" hook --rule quality-gate); then
  deny "Safety bridge failure (quality-gate) - blocked fail-closed."
fi

[ -n "$OUTPUT" ] && printf '%s\n' "$OUTPUT"

# CWD extraction from PreToolUse payload (Phase 7.x hygiene fix).
# Previously we used REPO_ROOT fisso = workspace main → worktree commits were
# blocked when main had unrelated staged files. Now we honour payload.cwd after
# realpath + allow-list validation (prevents fake-repo bypass, security MEDIUM).
CWD_RESOLVED=$(printf '%s' "$INPUT" | REPO_ROOT="$REPO_ROOT" python3 -c "
import sys, json, os
try:
    d = json.load(sys.stdin)
    cwd_raw = d.get('cwd') or os.environ.get('REPO_ROOT', '')
    if not cwd_raw:
        sys.exit(0)
    cwd = os.path.realpath(cwd_raw)
    # Allow-list = REPO_ROOT (where \$CLAUDE_PROJECT_DIR points) + any extra
    # worktree dirs declared in MARVIS_WORKTREE_DIRS (os.pathsep list). De-hardcoded
    # (S2 F2): no internal path baked into the shipped hook. On the Marvis server,
    # the worktree dir is injected via the .claude/settings.json env block (so
    # existing worktree commits stay allowed); OSS users (env unset) get REPO_ROOT
    # only — they work in the repo, not external worktrees.
    allowed = tuple(
        os.path.realpath(p)
        for p in [
            os.environ.get('REPO_ROOT') or os.getcwd(),
            *os.environ.get('MARVIS_WORKTREE_DIRS', '').split(os.pathsep),
        ]
        if p
    )
    if not any(cwd == a or cwd.startswith(a + os.sep) for a in allowed):
        sys.exit(0)
    print(cwd)
except Exception:
    pass
" 2>/dev/null || true)

CWD_RESOLUTION_FAILED=0
if [ -z "$CWD_RESOLVED" ]; then
  CWD_RESOLVED="$REPO_ROOT"
  CWD_RESOLUTION_FAILED=1
fi

# Consolidated git diff --cached call (julik P1). Previously migration check
# and MCP check ran two separate subprocess.run invocations → race window +
# double cost. Now one call, shared between both checks.
# Fail-closed philosophy: only run if `git commit` is the actual command; any
# inability to resolve the repository or staged diff blocks that commit.
STAGED_FILES=""
CMD_KIND=$(printf '%s' "$INPUT" | python3 -c "
import sys, json, shlex
try:
    d = json.load(sys.stdin)
    cmd = d.get('tool_input', {}).get('command', '') or ''
    lexer = shlex.shlex(cmd, posix=True, punctuation_chars=';&|')
    lexer.whitespace_split = True
    lexer.commenters = ''
    tokens = list(lexer)
    for index, token in enumerate(tokens):
        if token != 'git':
            continue
        for candidate in tokens[index + 1:]:
            if candidate and all(character in ';&|' for character in candidate):
                break
            if candidate == 'commit':
                print('commit')
                raise SystemExit
except Exception:
    pass
" 2>/dev/null || true)

if [ "$CMD_KIND" = "commit" ] && [ "$CWD_RESOLUTION_FAILED" -ne 0 ]; then
  deny "Quality gate could not validate the commit working directory; blocked fail-closed."
fi

if [ "$CMD_KIND" = "commit" ]; then
  if ! GIT_ROOT=$(git -C "$CWD_RESOLVED" rev-parse --show-toplevel 2>/dev/null); then
    deny "Quality gate could not resolve the commit repository; blocked fail-closed."
  fi
  CWD_RESOLVED="$GIT_ROOT"
  if ! STAGED_FILES=$(cd "$CWD_RESOLVED" && git diff --cached --name-only 2>/dev/null); then
    deny "Quality gate could not read staged files in $CWD_RESOLVED; blocked fail-closed."
  fi
fi

# Migration version gate: run when staging SQL migration files
STAGED_MIGRATIONS=$(printf '%s' "$STAGED_FILES" | python3 -c "
import sys
staged = [line for line in sys.stdin.read().splitlines() if line]
migrations = [f for f in staged if f.startswith('migrations/') and f.endswith('.sql')]
if migrations:
    print(' '.join(migrations))
" 2>/dev/null || true)

if [[ -n "$STAGED_MIGRATIONS" ]]; then
  MIG_CHECK="$CWD_RESOLVED/core/scripts/check-migration-version.sh"
  if [[ -x "$MIG_CHECK" ]]; then
    if ! (cd "$CWD_RESOLVED" && bash "$MIG_CHECK" 2>&1); then
      deny "Migration version gate failed. Local migration version must be > production. See core/scripts/check-migration-version.sh output above."
    fi
  else
    deny "Migration version gate dependency is missing or not executable: core/scripts/check-migration-version.sh."
  fi
fi

# MCP smoke test (S2 F2) — learning f2663d51: a schema/registration crash makes
# tools/list return fewer tools than baseline silently; all MCP clients then see
# a degraded tool set. We catch it BEFORE the commit lands. S1 moved the server
# from Node (mcp-pir/index.mjs) to Python (core/api/mcp/), so we trigger on staged
# files under core/api/mcp/ and round-trip the Python server in-process.
#
# MARVIS_MCP_MIN_TOOLS = baseline assertion (default 61, the S1 OSS baseline).
# NOT hardcoded to 91 — the OSS server ships fewer tools by design.
#
# This expensive smoke runs only when MCP Python files are staged. Once selected,
# every dependency is required: an unavailable SDK cannot become a green bypass.
STAGED_MCP=$(printf '%s' "$STAGED_FILES" | python3 -c "
import sys
staged = [line for line in sys.stdin.read().splitlines() if line]
mcp_files = [f for f in staged if f.startswith('core/api/mcp/') and f.endswith('.py')]
if mcp_files:
    print(' '.join(mcp_files))
" 2>/dev/null || true)

if [[ -n "$STAGED_MCP" ]]; then
  MIN_TOOLS="${MARVIS_MCP_MIN_TOOLS:-61}"
  # One in-process round-trip: import the server module + list_tools(). Emits
  # exactly one token on stdout: an integer tool count or FAIL:<reason>.
  SMOKE=$(cd "$CWD_RESOLVED" && MARVIS_OSS_LOCAL=1 python3 -c "
import sys, asyncio
try:
    import mcp  # the FastMCP SDK — required for this selected smoke
except ImportError as exc:
    print('FAIL:required mcp SDK unavailable'); sys.exit(0)
try:
    from core.api.mcp.server import mcp as server
except ImportError as exc:
    print('FAIL:required MCP server dependency unavailable: ' + type(exc).__name__); sys.exit(0)
except Exception as exc:  # syntax/registration error inside the server module
    print('FAIL:' + repr(exc)[:300]); sys.exit(0)
try:
    tools = asyncio.run(server.list_tools())
    print(len(tools))
except Exception as exc:
    print('FAIL:' + repr(exc)[:300])
" 2>/dev/null || echo 'FAIL:python smoke execution failed')

  case "$SMOKE" in
    FAIL:*)
      deny "MCP smoke test: 'import core.api.mcp.server' failed → ${SMOKE#FAIL:}. The MCP server module is broken; fix before commit (see learning f2663d51)." ;;
    ''|*[!0-9]*)
      deny "MCP smoke test returned an invalid result; blocked fail-closed." ;;
    [0-9]*)
      if [[ "$SMOKE" -lt "$MIN_TOOLS" ]]; then
        deny "MCP smoke test: tools/list returned $SMOKE tools (expected >= $MIN_TOOLS, the S1 OSS baseline). Likely a tool registration crash — see learning f2663d51."
      fi ;;
  esac
fi

exit 0
