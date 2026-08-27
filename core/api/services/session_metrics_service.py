# v1.2.0 - 2026-04-23 - PR4: compute_cost_session returns (real, equivalent, version, is_complete)
# v1.1.0 - 2026-04-22 - PR2: resume chain tracking (session_conversations) + cost_session aggregation
# v1.0.0 - 2026-04-22 - Single entry point for metrics refresh + memoization (PR1)
"""SessionMetricsService — single entry point for per-session metrics refresh.

Both the `api/main.py` maintenance loop and the `api/routers/sessions.py`
inline paths dispatch through `session_metrics_service.refresh(row)` so we
don't have ~30 duplicated state machines drifting from each other.

Responsibilities:
  1. Validate provider + conversation_id format.
  2. Dispatch to the right provider off-loop (asyncio.to_thread).
  3. Memoize cost lookups by (conv_id, file_mtime, file_size, provider) —
     historical conversations are immutable after exit, so the hit rate is
     very high for periodic pollers.
  4. Open a 60-second circuit breaker after 3 consecutive failures per
     session name so one flaky session can't dominate the loop.
  5. PR2: track resume chain in `session_conversations` table, aggregate
     `cost_session_usd` across the chain.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import defaultdict
from datetime import datetime, timezone
from functools import lru_cache

from core.api.services.metrics_providers import get_metrics_provider
from core.api.services.claude_metrics import SessionMetrics

logger = logging.getLogger(__name__)

# Format validators per provider — skip dispatch if mismatched.
_CLAUDE_CONV_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
_OPENCODE_CONV_RE = re.compile(r"^ses_[A-Za-z0-9]+$")

_CIRCUIT_COOLDOWN_SEC = 60.0
_CIRCUIT_FAILURE_THRESHOLD = 3


def _is_valid_conversation_id(provider: str, conv_id: str) -> bool:
    if provider == "claude":
        return bool(_CLAUDE_CONV_RE.match(conv_id))
    if provider == "codex":
        return bool(_CLAUDE_CONV_RE.match(conv_id))
    if provider == "opencode":
        return bool(_OPENCODE_CONV_RE.match(conv_id))
    # Unknown provider: accept any non-empty string; dispatch will decide.
    return bool(conv_id)


class SessionMetricsService:
    """Per-instance refresh dispatcher with circuit breaker."""

    def __init__(self) -> None:
        # consecutive_failures[name] resets to 0 on any success
        self._consecutive_failures: dict[str, int] = defaultdict(int)
        # skip_until[name] = epoch seconds until which we ignore this session
        self._skip_until: dict[str, float] = {}

    async def refresh(self, row: dict) -> SessionMetrics | None:
        """Refresh metrics for a session row (as returned from sessions_meta).

        Returns None if:
          - session is in circuit-breaker cooldown
          - provider unknown
          - conversation_id missing / format-invalid
          - provider returned None (expected-missing, e.g. DB locked)

        Re-raises on unexpected exceptions so the caller can log context;
        increments failure counter + opens breaker at threshold.
        """
        name = row.get("name", "<unknown>")
        provider_name = row.get("provider") or "claude"
        conv_id = row.get("conversation_id")

        if time.time() < self._skip_until.get(name, 0):
            return None

        if not conv_id:
            return None

        mp = get_metrics_provider(provider_name)
        if mp is None:
            logger.debug("No provider registered for %r (session %s)", provider_name, name)
            return None

        if not _is_valid_conversation_id(provider_name, conv_id):
            logger.warning(
                "Invalid conversation_id for %s (provider=%s): %r",
                name,
                provider_name,
                conv_id,
            )
            return None

        cwd = row.get("cwd")
        try:
            metrics = await asyncio.to_thread(mp.parse_session, conv_id, cwd)
        except Exception:
            self._record_failure(name)
            logger.exception(
                "Metrics refresh failed for %s (provider=%s conv=%s)",
                name,
                provider_name,
                conv_id,
            )
            raise

        # Success — reset counter whether or not metrics is None (None = expected)
        self._consecutive_failures[name] = 0
        self._skip_until.pop(name, None)
        return metrics

    # ------------------------------------------------------------------
    # Internal state transitions
    # ------------------------------------------------------------------

    def _record_failure(self, name: str) -> None:
        self._consecutive_failures[name] += 1
        if self._consecutive_failures[name] >= _CIRCUIT_FAILURE_THRESHOLD:
            self._skip_until[name] = time.time() + _CIRCUIT_COOLDOWN_SEC
            logger.warning(
                "Circuit breaker OPEN for %s (%.0fs cooldown)",
                name,
                _CIRCUIT_COOLDOWN_SEC,
            )

    # ------------------------------------------------------------------
    # Test helpers
    # ------------------------------------------------------------------

    def reset(self) -> None:
        """Wipe all circuit-breaker state (tests only)."""
        self._consecutive_failures.clear()
        self._skip_until.clear()


# --------------------------------------------------------------------------
# Module-level cost memoization
# --------------------------------------------------------------------------


@lru_cache(maxsize=500)
def parse_conversation_cost_memo(
    conv_id: str,
    file_mtime: float,
    file_size: int,
    provider: str,
) -> float | None:
    """Memoized cost lookup keyed by (conv_id, mtime, size, provider).

    Historical conversations are immutable after exit → once parsed, the cost
    doesn't change. Keying by (mtime, size) invalidates automatically when the
    underlying file changes. Expected hit rate 95%+ for active pollers.

    NOTE: `file_mtime` and `file_size` are cache keys, not read inputs — the
    caller is responsible for stat'ing the file first and passing the values
    so concurrent writers can't sneak past the cache.
    """
    mp = get_metrics_provider(provider)
    if mp is None:
        return None
    metrics = mp.parse_session(conv_id)
    if metrics is None:
        return None
    return metrics.cost_usd


# --------------------------------------------------------------------------
# Resume chain tracking (PR2)
# --------------------------------------------------------------------------


async def _resolve_session_workspace(
    db,
    session_name: str,
    workspace_id: str | None,
) -> str | None:
    """Resolve one exact parent workspace; never invent a tenant identity.

    ``workspace_id=None`` is a temporary compatibility path for callers that
    have not threaded identity yet.  It is safe only because sessions_meta.name
    is the concrete parent: missing, blank, or mismatched ownership fails
    closed.  Remote callers should always pass the workspace explicitly.
    """
    if workspace_id is not None and not workspace_id.strip():
        return None
    cursor = await db.execute(
        "SELECT workspace_id FROM sessions_meta "
        "WHERE name = ? AND workspace_id IS NOT NULL "
        "AND length(trim(workspace_id)) > 0",
        (session_name,),
    )
    rows = await cursor.fetchall()
    if len(rows) != 1:
        return None
    row = rows[0]
    resolved = row["workspace_id"] if isinstance(row, dict) or hasattr(row, "keys") else row[0]
    if workspace_id is not None and resolved != workspace_id:
        return None
    return str(resolved)


async def on_conversation_id_changed(
    db,
    session_name: str,
    new_conv_id: str,
    *,
    workspace_id: str | None = None,
) -> None:
    """Append a conversation_id to the session's resume chain.

    Called from the maintenance loop / router when `sessions_meta.conversation_id`
    changes for a session. The composite conflict target
    ``(workspace_id, session_name, conversation_id)`` makes repeated calls for
    the same tenant-scoped pair no-ops.

    Caller passes an aiosqlite connection (write_db or write_pool session) —
    this helper does not open its own transaction so it composes with the
    caller's batch.
    """
    if not session_name or not new_conv_id:
        return
    try:
        resolved_workspace = await _resolve_session_workspace(
            db, session_name, workspace_id
        )
        if resolved_workspace is None:
            logger.warning(
                "Refusing resume-chain append without exact workspace "
                "(name=%s requested_workspace=%s)",
                session_name,
                workspace_id,
            )
            return
        await db.execute(
            "INSERT INTO session_conversations "
            "(workspace_id, session_name, conversation_id, ord, created_at) "
            "VALUES (?, ?, ?, "
            "COALESCE((SELECT MAX(ord)+1 FROM session_conversations "
            "WHERE workspace_id=? AND session_name=?), 0), ?) "
            "ON CONFLICT(workspace_id, session_name, conversation_id) DO NOTHING",
            (
                resolved_workspace,
                session_name,
                new_conv_id,
                resolved_workspace,
                session_name,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
    except Exception:  # noqa: BLE001 - logged, not raised
        logger.exception(
            "Failed to append session_conversations (name=%s conv=%s)",
            session_name,
            new_conv_id,
        )


async def compute_cost_session(
    db,
    session_name: str,
    provider: str,
    *,
    workspace_id: str | None = None,
) -> tuple[float, bool]:
    """Aggregate cost across all conversation_ids for a session.

    Thin wrapper that discards the equivalent-cost fields from
    `compute_cost_session_extended` so PR2 callers keep the 2-tuple contract.
    New code (PR4+) should use the extended variant directly.
    """
    total, _equivalent, _version, is_complete = await compute_cost_session_extended(
        db, session_name, provider, workspace_id=workspace_id
    )
    return total, is_complete


async def compute_cost_session_extended(
    db,
    session_name: str,
    provider: str,
    *,
    workspace_id: str | None = None,
) -> tuple[float, float | None, str | None, bool]:
    """Aggregate real + shadow cost across all conversation_ids for a session.

    Reads `session_conversations` in `ord` order, calls the provider for each
    conversation, sums `cost_conversation_usd` and
    `cost_conversation_equivalent_usd`. Returns
    `(total_real, total_equivalent, equivalent_version, is_complete)`:

    - `total_real` — sum of real costs (0 for OAuth/free sessions)
    - `total_equivalent` — sum of shadow costs (pay-per-token API), or None
      if NO conversation in the chain had known pricing
    - `equivalent_version` — pricing version tag from the parser (audit)
    - `is_complete` — False when any conversation is missing / parse failed

    NOTE: I/O-intensive for long chains — caller should run it off-loop.
    """
    resolved_workspace = await _resolve_session_workspace(
        db, session_name, workspace_id
    )
    if resolved_workspace is None:
        return 0.0, None, None, False

    cursor = await db.execute(
        "SELECT conversation_id FROM session_conversations "
        "WHERE workspace_id=? AND session_name=? ORDER BY ord",
        (resolved_workspace, session_name),
    )
    rows = await cursor.fetchall()
    conv_ids = [
        r["conversation_id"]
        if isinstance(r, dict) or hasattr(r, "keys")
        else r[0]
        for r in rows
    ]

    if not conv_ids:
        return 0.0, None, None, True

    mp = get_metrics_provider(provider)
    if mp is None:
        return 0.0, None, None, True

    total_real = 0.0
    total_equivalent = 0.0
    equivalent_seen = False
    equivalent_version: str | None = None
    is_complete = True
    for ci in conv_ids:
        try:
            m = await asyncio.to_thread(mp.parse_session, ci, None)
        except Exception:
            logger.warning("cost_session: parse failed for %s", ci, exc_info=True)
            is_complete = False
            continue
        if m is None:
            is_complete = False
            continue
        total_real += float(m.cost_conversation_usd or m.cost_usd or 0.0)
        eq = m.cost_conversation_equivalent_usd
        if eq is not None:
            equivalent_seen = True
            total_equivalent += float(eq)
            if equivalent_version is None:
                equivalent_version = m.cost_equivalent_pricing_version

    equivalent: float | None = round(total_equivalent, 4) if equivalent_seen else None
    return round(total_real, 4), equivalent, equivalent_version, is_complete


# Module singleton — import as `from api.services.session_metrics_service
# import session_metrics_service`.
session_metrics_service = SessionMetricsService()
