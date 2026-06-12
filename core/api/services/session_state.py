# v1.0.0 - 2026-04-26 - Session state event mapping (provider hooks → canonical state)
"""Provider-agnostic session activity_state derivation from event hooks/plugins.

Replaces TUI text scraping (api/services/tmux.py:detect_activity_state) with
event-driven push from Claude Code hooks (~/.claude/settings.json) and
OpenCode plugin (~/.config/opencode/plugins/marvisx-state.ts). Global
session-list reads no longer capture panes when events are stale/missing.

Plan: docs/plans/2026-04-26-feat-session-state-event-driven-plan.md
Research: docs/research/2026-04-26-session-turn-lifecycle-providers.md
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Literal

import aiosqlite

logger = logging.getLogger(__name__)

# 5 canonical states (4 active + ended for lifecycle audit).
CanonicalState = Literal["idle", "working", "needs_input", "error", "ended"]

# Provider event → canonical state. Table-driven so adding Codex/Gemini in
# Phase 2 is one row each. Key format: (provider, event_string).
EVENT_MAP: dict[tuple[str, str], CanonicalState] = {
    # Claude Code hooks (https://docs.claude.com/en/docs/claude-code/hooks)
    ("claude", "PreToolUse"): "working",
    ("claude", "Stop"): "idle",
    ("claude", "StopFailure"): "error",
    ("claude", "PermissionRequest"): "needs_input",
    ("claude", "SessionStart"): "idle",
    ("claude", "SessionEnd"): "ended",
    # OpenCode plugin events (https://opencode-tutorial.com/en/docs/plugins)
    # session.status is canonical; session.idle is deprecated alias kept for
    # backward compat with older OpenCode versions still emitting it.
    ("opencode", "session.status:active"): "working",
    ("opencode", "session.status:idle"): "idle",
    ("opencode", "session.status:error"): "error",
    ("opencode", "session.error"): "error",
    ("opencode", "session.idle"): "idle",  # deprecated alias
    ("opencode", "session.deleted"): "ended",
    ("opencode", "permission.updated"): "needs_input",
}

# Reject events with client ts more than this in the future (anti-spam).
# 5 min covers reasonable clock skew on a single host.
MAX_FUTURE_SKEW = timedelta(minutes=5)
# Drop events older than this — likely retransmit of a long-stale event.
MAX_PAST_AGE = timedelta(hours=1)


def map_event_to_state(provider: str, event: str) -> CanonicalState | None:
    """Return the canonical state for a (provider, event) tuple.

    Returns None if the event is not recognized — caller should ignore the
    event silently (e.g., session.compacted, message.updated). This is an
    explicit allow-list; arbitrary events never reach the DB.
    """
    return EVENT_MAP.get((provider, event))


def parse_client_ts(ts_str: str) -> datetime | None:
    """Parse client-emitted ISO timestamp, validate against future/past bounds.

    Returns parsed UTC datetime or None if rejected. Rejection logged at WARN
    so a misbehaving hook is observable.
    """
    try:
        # Accept "2026-04-26T14:30:00.123456Z" and "+00:00" suffix variants.
        normalized = ts_str.replace("Z", "+00:00")
        ts = datetime.fromisoformat(normalized)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
    except (ValueError, AttributeError) as exc:
        logger.warning("session_state: invalid ts %r: %s", ts_str, exc)
        return None
    now = datetime.now(timezone.utc)
    if ts > now + MAX_FUTURE_SKEW:
        logger.warning(
            "session_state: rejecting future ts %s (now=%s, skew>%s)",
            ts.isoformat(),
            now.isoformat(),
            MAX_FUTURE_SKEW,
        )
        return None
    if now - ts > MAX_PAST_AGE:
        logger.info(
            "session_state: dropping stale ts %s (age>%s)",
            ts.isoformat(),
            MAX_PAST_AGE,
        )
        return None
    return ts


async def resolve_session_name(
    db: aiosqlite.Connection, identifier: str
) -> str | None:
    """Resolve `{identifier}` path param to canonical sessions_meta.name.

    Single endpoint accepts both name (e.g. `marvis`) and conversation_id
    (UUID for Claude, `ses_*` for OpenCode). UUID-shaped → lookup by
    conversation_id. Else assume tmux name.

    Returns None if no row matches.
    """
    # UUID v4 shape (Claude conversation_id) or `ses_<base64>` (OpenCode).
    is_uuid_like = (
        len(identifier) == 36 and identifier.count("-") == 4
    ) or identifier.startswith("ses_")
    if is_uuid_like:
        cur = await db.execute(
            "SELECT name FROM sessions_meta WHERE conversation_id = ?",
            (identifier,),
        )
        row = await cur.fetchone()
        if row:
            return row[0]
        # Fall through: maybe it's a session name that happens to look UUID-y
    cur = await db.execute(
        "SELECT name FROM sessions_meta WHERE name = ?",
        (identifier,),
    )
    row = await cur.fetchone()
    return row[0] if row else None


async def record_state_event(
    db: aiosqlite.Connection,
    session_name: str,
    provider: str,
    event: str,
    client_ts: datetime,
) -> CanonicalState | None:
    """Apply the event to sessions_meta.activity_state.

    Idempotent last-write-wins on `activity_state_updated_at` using the
    CLIENT timestamp (julik R2 fix for out-of-order arrival between uvicorn
    workers). The UPDATE skips if a fresher event already won.

    Returns the canonical state that was applied (or None if event was
    ignored / lost the LWW race).
    """
    state = map_event_to_state(provider, event)
    if state is None:
        return None

    ts_iso = client_ts.isoformat()
    cursor = await db.execute(
        """
        UPDATE sessions_meta
           SET activity_state = ?,
               activity_state_updated_at = ?
         WHERE name = ?
           AND (activity_state_updated_at IS NULL
                OR activity_state_updated_at < ?)
        """,
        (state, ts_iso, session_name, ts_iso),
    )
    if cursor.rowcount == 0:
        # Either session not found, or LWW race lost. Surface at DEBUG —
        # not an error per se; broadcast skipped by caller.
        logger.debug(
            "session_state: no-op update for %s (%s/%s ts=%s)",
            session_name,
            provider,
            event,
            ts_iso,
        )
        return None
    return state
