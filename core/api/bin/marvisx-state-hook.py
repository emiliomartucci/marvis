#!/usr/bin/env python3
# v1.0.0 - 2026-04-26 - Claude Code session state hook (PR2 plan 2026-04-26)
"""Push Claude Code session lifecycle events to MarvisX backend.

Wired to ~/.claude/settings.json hooks (PreToolUse, Stop, StopFailure,
PermissionRequest, SessionStart, SessionEnd). Reads the Claude hook JSON
payload from stdin, resolves the tmux session name, POSTs to
`/api/v1/sessions/{name}/state`.

Design choices (plan §M2/M3/M5/M6):

- **Python over bash** (M5): single-process startup ~25ms vs bash+jq
  fork chain at 80-150ms. Anthropic's <50ms PreToolUse target is reachable.

- **fork + setsid + urllib** (M3): the parent process exits immediately so
  Claude's hook completes fast. The child detaches from the parent's process
  group via `os.setsid()` and uses urllib in-process (no curl exec, no token
  leaking into argv visible in `ps -ef`). Survives parent SIGHUP — critical
  for `Stop`/`SessionEnd` events fired during Claude shutdown.

- **Token from file, not env** (M6): `~/.marvisx/agent-token` (mode 0600).
  Env vars leak via `/proc/<pid>/environ` to other users on shared machines.

- **Client-emitted ts** (M2): the backend uses this as the LWW key so that
  out-of-order arrival between uvicorn workers doesn't leave the session
  stuck in a stale state.

- **Fail silent**: any error → exit 0. Hooks are fire-and-forget; surfacing
  errors to Claude would be more noise than signal.
"""
from __future__ import annotations

import json
import os
import socket
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib import error, request

API_URL = os.environ.get("MARVISX_API_URL", "http://127.0.0.1:8100")
TOKEN_FILE = Path.home() / ".marvisx" / "agent-token"
TIMEOUT_SECS = 5


def _read_token() -> str | None:
    try:
        if not TOKEN_FILE.exists():
            return None
        st = TOKEN_FILE.stat()
        # Defense-in-depth: warn on loose perms but don't fail.
        if st.st_mode & 0o077:
            print(
                f"[marvisx-state-hook] WARN: {TOKEN_FILE} permissive (chmod 600)",
                file=sys.stderr,
            )
        return TOKEN_FILE.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def _resolve_session_name() -> str | None:
    """Find the tmux session this hook is running inside.

    Priority:
    1. `TMUX_SESSION_NAME` env (exported by the launcher, plan §M9)
    2. `MARVISX_SESSION_NAME` env (alternative explicit override)
    3. `tmux display -p '#{session_name}'` if `$TMUX` is set
    """
    name = os.environ.get("TMUX_SESSION_NAME") or os.environ.get(
        "MARVISX_SESSION_NAME"
    )
    if name:
        return name
    if not os.environ.get("TMUX"):
        return None
    import subprocess

    try:
        result = subprocess.run(
            ["tmux", "display", "-p", "#{session_name}"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        if result.returncode == 0:
            stripped = result.stdout.strip()
            return stripped or None
    except (OSError, subprocess.SubprocessError):
        return None
    return None


def _post(payload: dict, token: str, session_name: str) -> None:
    """Send POST to backend. Called from the detached child only."""
    url = f"{API_URL}/api/v1/sessions/{session_name}/state"
    body = json.dumps(payload).encode("utf-8")
    req = request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
            "X-Agent-Name": "marvisx",
        },
    )
    try:
        with request.urlopen(req, timeout=TIMEOUT_SECS) as resp:  # noqa: S310
            resp.read()
    except (error.URLError, error.HTTPError, OSError, socket.timeout):
        pass  # silent — hook is fire-and-forget


def main() -> int:
    try:
        raw = sys.stdin.read()
    except OSError:
        return 0
    if not raw:
        return 0
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return 0

    event = data.get("hook_event_name")
    if not event:
        return 0

    session_name = _resolve_session_name()
    if not session_name:
        return 0

    token = _read_token()
    if not token:
        return 0

    payload = {
        "provider": "claude",
        "event": event,
        "conv_id": data.get("session_id"),
        "ts": datetime.now(timezone.utc).isoformat(),
    }

    # Fork-and-detach so the parent (hook) returns to Claude immediately and
    # the child completes the POST even if Claude exits (Stop/SessionEnd
    # races, julik R3). Posix-only — Marvis is Linux.
    try:
        pid = os.fork()
    except OSError:
        # Couldn't fork — last-resort inline POST.
        _post(payload, token, session_name)
        return 0

    if pid > 0:
        # Parent: hook returns success immediately.
        return 0

    # Child: detach from parent's session/process group + close inherited
    # std streams to avoid keeping pipes alive after the parent exits.
    try:
        os.setsid()
    except OSError:
        pass
    try:
        for fd in (0, 1, 2):
            os.close(fd)
        devnull = os.open(os.devnull, os.O_RDWR)
        os.dup2(devnull, 0)
        os.dup2(devnull, 1)
        os.dup2(devnull, 2)
    except OSError:
        pass

    _post(payload, token, session_name)
    os._exit(0)


if __name__ == "__main__":
    sys.exit(main())
