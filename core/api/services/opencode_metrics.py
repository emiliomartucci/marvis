# v1.1.0 - 2026-04-23 - PR4: per-message shadow cost_equivalent via kb/opencode-pricing-*.json
# v1.0.0 - 2026-04-22 - OpenCodeMetricsProvider: SQLite parser for cost/ctx/tokens (PR1)
"""OpenCode metrics parser.

Reads `~/.local/share/opencode/opencode.db` (writer lives outside our
process) via a read-only SQLite connection and assembles a SessionMetrics
compatible with the Claude shape.

Safety / correctness notes (see plan §Phase 1):

- `immutable=0` on the readonly URI — OpenCode is actively writing, and
  `immutable=1` would disable WAL locking and return inconsistent pages.
- Transactions kept <100ms so we don't pin WAL checkpoint.
- `session_id` validated with a strict regex BEFORE any path/query use to
  prevent traversal or injection.
- `OperationalError` with 'database is locked' / 'SQLITE_BUSY' → return None
  (caller falls back to cached metrics). Other SQL errors propagate.
- Explicit `conn.close()` in `finally` — no handle leak on exception.
"""
from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from core.api.services.claude_metrics import SessionMetrics
from core.api.services.model_registry import (
    context_window,
    normalize_model_id,
    opencode_pricing,
    opencode_pricing_version,
)

logger = logging.getLogger(__name__)

OPENCODE_DB_PATH = Path(
    os.getenv("OPENCODE_DB_PATH", "~/.local/share/opencode/opencode.db")
).expanduser()

# Strict format: OpenCode session IDs always match ^ses_[A-Za-z0-9]+$
# (see opencode_sessions.py). Validate BEFORE any use — defense in depth.
OPENCODE_SESSION_ID_RE = re.compile(r"^ses_[A-Za-z0-9]+$")


def _epoch_ms_to_iso(value: int | float | None) -> str | None:
    """Convert OpenCode time_created (epoch ms) to ISO 8601 UTC string."""
    if value is None:
        return None
    try:
        dt = datetime.fromtimestamp(float(value) / 1000.0, tz=timezone.utc)
    except (ValueError, TypeError, OSError):
        return None
    return dt.isoformat()


def _safe_int(value, default: int = 0) -> int:
    """Coerce to int, tolerating None and floats from JSON."""
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


class OpenCodeMetricsProvider:
    """MetricsProvider for OpenCode SQLite-backed sessions."""

    name = "opencode"

    def parse_session(
        self,
        session_id: str,
        cwd: str | None = None,
    ) -> SessionMetrics | None:
        """Aggregate cost/tokens/context for an OpenCode session.

        Returns None if:
          - session_id fails regex validation
          - DB file doesn't exist (OpenCode not installed / new machine)
          - DB is locked (caller reads cached metrics)
          - session has no assistant messages yet
        """
        if not OPENCODE_SESSION_ID_RE.match(session_id):
            logger.warning("Invalid opencode session_id rejected: %r", session_id)
            return None

        if not OPENCODE_DB_PATH.exists():
            logger.debug("OpenCode DB missing at %s", OPENCODE_DB_PATH)
            return None

        # CRITICAL: immutable=0 — writer is active, immutable=1 returns
        # inconsistent pages (see plan §Phase 1 F1).
        uri = f"file:{OPENCODE_DB_PATH}?mode=ro&immutable=0"
        conn: sqlite3.Connection | None = None
        try:
            conn = sqlite3.connect(uri, uri=True, timeout=5.0)
            conn.execute("PRAGMA busy_timeout = 5000")
            conn.execute("PRAGMA query_only = 1")

            assistant_rows = conn.execute(
                "SELECT data, time_created FROM message "
                "WHERE session_id = ? "
                "AND json_extract(data, '$.role') = 'assistant' "
                "ORDER BY time_created",
                (session_id,),
            ).fetchall()

            # user messages too — needed for working_seconds pairing
            user_rows = conn.execute(
                "SELECT data, time_created FROM message "
                "WHERE session_id = ? "
                "AND json_extract(data, '$.role') = 'user' "
                "ORDER BY time_created",
                (session_id,),
            ).fetchall()
        except sqlite3.OperationalError as exc:
            msg = str(exc).lower()
            if "database is locked" in msg or "sqlite_busy" in msg:
                logger.debug(
                    "OpenCode DB busy, skip session %s: %s", session_id, exc
                )
                return None
            raise
        finally:
            if conn is not None:
                conn.close()

        if not assistant_rows:
            return None

        return self._build_metrics(session_id, assistant_rows, user_rows)

    def get_last_context_pct(
        self,
        session_id: str,
        cwd: str | None = None,
    ) -> float | None:
        """Fast last-message context % via the same SQLite path.

        OpenCode has no tail-read optimization distinct from full parse
        (rows are already ordered and bounded by session_id), so we reuse
        parse_session and return its computed context_pct.
        """
        metrics = self.parse_session(session_id, cwd)
        return metrics.context_pct if metrics else None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_metrics(
        self,
        session_id: str,
        assistant_rows: list[tuple],
        user_rows: list[tuple],
    ) -> SessionMetrics | None:
        """Compute aggregates from raw message rows.

        Rules (see plan §Phase 1):
          G1: ctx_tokens = max(tokens.total, sum of parts)
          G4: exclude finish in ('error', None) from working_seconds
          G9: skip negative gaps (clock skew)
          Cost: exclude entries with cost==0 AND finish!='stop' (error msgs)
        """
        cost_total = 0.0
        cost_equivalent_total = 0.0
        cost_equivalent_seen = False  # at least one message had known pricing
        input_total = 0
        output_total = 0
        reasoning_total = 0
        cache_read_total = 0
        cache_write_total = 0
        last_payload: dict | None = None
        last_model: str | None = None
        assistant_times: list[tuple[int, str | None]] = []  # (time_created_ms, finish)
        first_ts_ms: int | None = None
        last_ts_ms: int | None = None

        for raw, time_created in assistant_rows:
            try:
                payload = json.loads(raw) if isinstance(raw, str) else raw
            except (TypeError, json.JSONDecodeError):
                continue
            if not isinstance(payload, dict):
                continue

            if first_ts_ms is None:
                first_ts_ms = int(time_created) if time_created is not None else None
            if time_created is not None:
                last_ts_ms = int(time_created)

            finish = payload.get("finish")
            cost_val = payload.get("cost")
            # Skip genuinely error-shaped messages from cost sum. `finish` values
            # in OpenCode include "stop" (natural end), "tool-calls" (LLM emitted
            # a tool call — legitimate token spend), "length" (context limit hit),
            # "content-filter" (refusal — still billed). Only "error" and null
            # are actual failures with no tokens billed.
            is_error_shaped = finish in ("error", None)
            if cost_val is not None:
                try:
                    cost_float = float(cost_val)
                except (TypeError, ValueError):
                    cost_float = 0.0
                if not is_error_shaped:
                    cost_total += cost_float

            tokens = payload.get("tokens") or {}
            msg_input = 0
            msg_output = 0
            msg_reasoning = 0
            msg_cache_read = 0
            msg_cache_write = 0
            if isinstance(tokens, dict):
                msg_input = _safe_int(tokens.get("input"))
                msg_output = _safe_int(tokens.get("output"))
                msg_reasoning = _safe_int(tokens.get("reasoning"))
                input_total += msg_input
                output_total += msg_output
                reasoning_total += msg_reasoning
                cache = tokens.get("cache") or {}
                if isinstance(cache, dict):
                    msg_cache_read = _safe_int(cache.get("read"))
                    msg_cache_write = _safe_int(cache.get("write"))
                    cache_read_total += msg_cache_read
                    cache_write_total += msg_cache_write

            model_id = payload.get("modelID")
            provider_id = payload.get("providerID")
            if model_id:
                last_model = model_id

            # PR4: per-message shadow cost. Skip ONLY genuinely error-shaped
            # messages (same filter as real cost: finish in ("error", None)).
            # tool-calls / length / content-filter all consume real tokens and
            # must be counted in shadow cost. Also skip when provider+model is
            # unknown (fallback_strategy=skip — never guess).
            if not is_error_shaped:
                p = opencode_pricing(provider_id, model_id)
                if p is not None:
                    cost_equivalent_seen = True
                    # OpenCode exposes a single `cache.write` bucket, no TTL
                    # split — map to 1h (most conservative pricing, matches
                    # existing real-cost convention).
                    cost_equivalent_total += (
                        msg_input * p.input
                        + msg_output * p.output
                        + msg_reasoning * p.output  # reasoning billed as output
                        + msg_cache_read * p.cache_read
                        + msg_cache_write * p.cache_write_1h
                    ) / 1_000_000

            last_payload = payload
            assistant_times.append((int(time_created or 0), finish))

        if last_payload is None:
            return None

        # G1: ctx_tokens = max(tokens.total, sum of parts)
        last_tokens = last_payload.get("tokens") or {}
        last_total = _safe_int(last_tokens.get("total"))
        last_cache = last_tokens.get("cache") or {} if isinstance(last_tokens, dict) else {}
        last_sum = (
            _safe_int(last_tokens.get("input"))
            + _safe_int(last_tokens.get("output"))
            + _safe_int(last_tokens.get("reasoning"))
            + _safe_int(last_cache.get("read") if isinstance(last_cache, dict) else 0)
            + _safe_int(last_cache.get("write") if isinstance(last_cache, dict) else 0)
        )
        last_ctx_tokens = max(last_total, last_sum)

        ctx_window = context_window(last_model)
        context_pct = round(last_ctx_tokens / ctx_window * 100, 1) if ctx_window else 0.0
        context_pct = min(context_pct, 100.0)

        # working_seconds_msg: pair each user message with the next assistant,
        # exclude error/None finishes, skip negative gaps.
        working_ms = self._compute_working_ms(user_rows, assistant_times)
        working_seconds_msg = int(working_ms // 1000) if working_ms > 0 else 0

        first_ts_iso = _epoch_ms_to_iso(first_ts_ms)
        last_ts_iso = _epoch_ms_to_iso(last_ts_ms)

        duration_minutes = 0.0
        if first_ts_ms is not None and last_ts_ms is not None and last_ts_ms > first_ts_ms:
            duration_minutes = round((last_ts_ms - first_ts_ms) / 1000.0 / 60.0, 1)

        normalized_model = normalize_model_id(last_model) or last_model

        # OpenCode doesn't split TTL in message.data.tokens.cache — report the
        # full `write` bucket as 1h (most conservative pricing). If provider
        # grows a split later, we can re-map; keeps cost a safe upper bound.
        cache_write_5m = 0
        cache_write_1h = cache_write_total

        cost_conversation = round(cost_total, 6)
        # PR4: equivalent only populated when at least one message had known
        # pricing; otherwise None (fallback_strategy=skip).
        if cost_equivalent_seen:
            cost_equivalent_conversation: float | None = round(cost_equivalent_total, 6)
            equivalent_version = opencode_pricing_version()
        else:
            cost_equivalent_conversation = None
            equivalent_version = None

        return SessionMetrics(
            conversation_id=session_id,
            model=normalized_model,
            # Legacy aliases
            context_pct=context_pct,
            cost_usd=cost_conversation,
            message_count=len(assistant_rows),
            input_tokens=input_total,
            output_tokens=output_total,
            cache_read_tokens=cache_read_total,
            cache_write_tokens=cache_write_total,
            first_timestamp=first_ts_iso,
            last_timestamp=last_ts_iso,
            duration_minutes=duration_minutes,
            # PR2 fields
            context_pct_real=context_pct,
            context_pct_scaled=None,  # OpenCode has no 84% auto-compact banner
            cost_conversation_usd=cost_conversation,
            cost_session_usd=cost_conversation,  # single-conv sessions for PR2
            cost_session_incomplete=False,
            cache_write_5m_tokens=cache_write_5m,
            cache_write_1h_tokens=cache_write_1h,
            reasoning_tokens=reasoning_total or None,
            working_seconds_msg=working_seconds_msg,
            pricing_version="2026-04-22",
            # PR4 shadow cost
            cost_conversation_equivalent_usd=cost_equivalent_conversation,
            cost_session_equivalent_usd=cost_equivalent_conversation,  # single-conv
            cost_equivalent_pricing_version=equivalent_version,
        )

    def _compute_working_ms(
        self,
        user_rows: list[tuple],
        assistant_times: list[tuple[int, str | None]],
    ) -> int:
        """Sum (assistant.time.completed - user.time.created) per pair.

        Falls back to message time_created when 'time' sub-object is absent.
        Excludes error/None finish and negative gaps (clock skew).
        """
        if not user_rows or not assistant_times:
            return 0

        user_created = sorted(
            int(t) for _, t in user_rows if t is not None
        )

        total_ms = 0
        a_idx = 0
        for u_ms in user_created:
            # Find the next assistant after this user
            while a_idx < len(assistant_times) and assistant_times[a_idx][0] < u_ms:
                a_idx += 1
            if a_idx >= len(assistant_times):
                break
            a_ms, finish = assistant_times[a_idx]
            a_idx += 1  # consume this assistant even if excluded
            if finish in (None, "error"):
                continue
            gap = a_ms - u_ms
            if gap <= 0:
                continue
            total_ms += gap
        return total_ms
