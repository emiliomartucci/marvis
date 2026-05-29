from __future__ import annotations

import asyncio
import re
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

OPENCODE_DB_PATH = Path.home() / ".local/share/opencode/opencode.db"
OPENCODE_SESSION_ID_RE = re.compile(r"^ses_[A-Za-z0-9]+$")
_BACKFILL_MAX_DELTA_MS = 15 * 60 * 1000
_LAUNCH_SKEW_MS = 3_000


def is_opencode_session_id(value: str | None) -> bool:
    return bool(value and OPENCODE_SESSION_ID_RE.match(value))


def _normalize_directory(directory: str) -> str:
    return str(Path(directory).expanduser().resolve())


def _iso_to_epoch_ms(value: str | None) -> int | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def _query_single_value(query: str, params: tuple[object, ...]) -> str | None:
    if not OPENCODE_DB_PATH.exists():
        return None
    conn = sqlite3.connect(OPENCODE_DB_PATH)
    try:
        row = conn.execute(query, params).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    value = row[0]
    return value if isinstance(value, str) else None


def find_session_id_for_created_at(
    directory: str,
    created_at: str | None,
    *,
    max_delta_ms: int = _BACKFILL_MAX_DELTA_MS,
) -> str | None:
    target_ms = _iso_to_epoch_ms(created_at)
    if target_ms is None:
        return None
    return _query_single_value(
        """
        SELECT id
        FROM session
        WHERE directory = ?
          AND ABS(time_created - ?) <= ?
        ORDER BY ABS(time_created - ?) ASC, time_created ASC
        LIMIT 1
        """,
        (_normalize_directory(directory), target_ms, max_delta_ms, target_ms),
    )


def find_new_session_id(
    directory: str,
    launched_at_ms: int,
) -> str | None:
    return _query_single_value(
        """
        SELECT id
        FROM session
        WHERE directory = ?
          AND time_created >= ?
        ORDER BY time_created ASC
        LIMIT 1
        """,
        (_normalize_directory(directory), launched_at_ms - _LAUNCH_SKEW_MS),
    )


async def wait_for_new_session_id(
    directory: str,
    launched_at_ms: int,
    *,
    timeout_seconds: float = 8.0,
    poll_interval: float = 0.25,
) -> str | None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        session_id = await asyncio.to_thread(
            find_new_session_id,
            directory,
            launched_at_ms,
        )
        if session_id:
            return session_id
        await asyncio.sleep(poll_interval)
    return None


def detect_opencode_for_session(
    directory: str,
    pane_start_ms: int | None = None,
    exclude_ids: list[str] | None = None,
) -> str | None:
    """Find the most recent OpenCode session_id matching a tmux pane cwd.

    Parity companion of `claude_metrics.detect_conversation_for_session` for
    OpenCode: used to wire up sessions that started OpenCode manually (user
    typed ``opencode`` inside an existing tmux session, bypassing the Console
    "New Session" modal flow). Without this detection, `sessions_meta.
    conversation_id` stays NULL and the parser emits zero metrics.

    Matches OpenCode DB ``session.directory`` against the tmux pane cwd,
    filters by ``time_created`` window (pane_start lower-bounded with the
    same skew used during new-session polling), and prefers the most recently
    active row (``ORDER BY time_updated DESC``). This handles multiple
    OpenCode sessions opened in the same cwd — we want the one the user is
    actually interacting with.

    Args:
        directory: tmux pane cwd (will be resolved/normalized).
        pane_start_ms: lower bound on ``time_created`` (epoch ms). If None,
            no lower bound (fallback for sessions without pane_start).
        exclude_ids: session ids already linked to other tmux sessions
            (avoids stealing another pane's conv_id when cwds coincide).

    Returns:
        session_id string, or None if no match / DB missing / locked.
    """
    if not OPENCODE_DB_PATH.exists():
        return None
    norm_dir = _normalize_directory(directory)

    query_parts = ["SELECT id FROM session WHERE directory = ?"]
    params: list = [norm_dir]
    if pane_start_ms is not None:
        # Filter on time_updated (last activity), NOT time_created.
        # `opencode --continue <ses_id>` resumes an existing session whose
        # time_created is days/weeks before the pane started — filtering
        # by time_created would reject the active resumed session. The
        # resume writes time_updated on each turn, so that field reliably
        # marks "alive during this pane's lifetime".
        query_parts.append("AND time_updated >= ?")
        params.append(pane_start_ms - _LAUNCH_SKEW_MS)
    if exclude_ids:
        placeholders = ",".join("?" * len(exclude_ids))
        query_parts.append(f"AND id NOT IN ({placeholders})")
        params.extend(exclude_ids)
    query_parts.append("ORDER BY time_updated DESC LIMIT 1")
    query = " ".join(query_parts)

    # CRITICAL: immutable=0 — writer is active, immutable=1 returns
    # inconsistent pages (matches opencode_metrics.parse_session).
    uri = f"file:{OPENCODE_DB_PATH}?mode=ro&immutable=0"
    try:
        with sqlite3.connect(uri, uri=True, timeout=5.0) as conn:
            conn.execute("PRAGMA busy_timeout = 5000")
            conn.execute("PRAGMA query_only = 1")
            row = conn.execute(query, params).fetchone()
    except sqlite3.OperationalError:
        return None
    if not row:
        return None
    return row[0] if isinstance(row[0], str) else None
