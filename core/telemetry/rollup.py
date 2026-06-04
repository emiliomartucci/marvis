# v1.0.0 - 2026-05-29 - open-core funnel Phase 0: daily aggregate rollup over console.db
"""Daily aggregate rollup over the LOCAL ``console.db`` — counts/sums only, no PII.

This is the data source for the personal-area dashboard (the open-core funnel,
docs/plans/2026-05-29-feat-oss-personal-area-metrics-funnel-plan.md). It is the
*aggregate* sibling of the event client (``client.py``): where ``emit()`` reports
discrete usage events, this reads the user's own console.db and produces one
small dict per day — sessions, cost, tokens, KG scale, task throughput, searches,
MCP calls. The ``sender`` posts these to ``/v1/ingest`` for the user's dashboard.

The no-content guarantee is enforced HERE, by construction, the same way
``schema.py`` enforces the event whitelist:

- :data:`ROLLUP_KEYS` is the EXACT set of keys a day payload may carry. Anything
  else → :class:`RollupError`. Adding a metric is a deliberate code change here,
  reviewed against the no-content bar.
- Every value is an int/float count or sum, except ``day`` (a UTC date string)
  and ``kg_nodes_by_type`` (a dict of ``{node_type: int}`` whose keys are the
  fixed, low-cardinality ``graph_nodes.type`` enum). We NEVER ship a path, slug,
  title, query, filename, or any free text. :func:`validate_day` proves it and a
  unit test pins it.

Read-only + defensive by construction: the DB is opened ``mode=ro``; every query
is wrapped so a missing table/column (older or partial schema) yields ``0`` rather
than raising. Computing the rollup can never write to, lock, or corrupt the
user's DB, and never crashes the caller.
"""
from __future__ import annotations

import re
from typing import Any

# ---------------------------------------------------------------------------
# The allowlist (the no-content contract). Mirror of schema.py::EVENT_PROPS.
# ---------------------------------------------------------------------------

ROLLUP_KEYS: frozenset[str] = frozenset(
    {
        "day",  # UTC YYYY-MM-DD (the only string; a date, never free text)
        # time-series, attributed per day
        "sessions",
        "active_min",
        "cost_usd",
        "tokens_in",
        "tokens_out",
        "tasks_created",
        "tasks_completed",
        "searches",
        "mcp_calls",
        # KG scale gauges — a "now" snapshot, carried only on the current day's row
        "kg_nodes",
        "kg_nodes_by_type",
        "kg_edges",
        "kg_growth_7d",
    }
)

# A node-type token (key of kg_nodes_by_type) must look like the fixed
# graph_nodes.type enum — a short lowercase identifier. This blocks any path /
# free-text from ever leaking through a rogue grouping key.
_TYPE_TOKEN = re.compile(r"^[a-z][a-z0-9_]{0,31}$")


class RollupError(ValueError):
    """Raised when a day payload carries a key/value outside the allowlist."""


def validate_day(day: dict[str, Any]) -> dict[str, Any]:
    """Validate one day payload against the allowlist. Returns it unchanged.

    Raises :class:`RollupError` if a key is outside :data:`ROLLUP_KEYS`, a value
    has the wrong type, or a ``kg_nodes_by_type`` key is not a safe enum token.
    This is the single enforcement point the no-PII rollup test pins.
    """
    if not isinstance(day, dict):
        raise RollupError(f"day payload must be a dict, got {type(day).__name__}")
    extra = set(day) - ROLLUP_KEYS
    if extra:
        raise RollupError(
            f"rollup keys {sorted(extra)} are NOT in the allowlist "
            f"{sorted(ROLLUP_KEYS)} — the no-content guarantee is by construction"
        )
    for key, value in day.items():
        if key == "day":
            if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                raise RollupError(f"'day' must be a YYYY-MM-DD string, got {value!r}")
        elif key == "kg_nodes_by_type":
            if not isinstance(value, dict):
                raise RollupError("'kg_nodes_by_type' must be a dict")
            for tk, tv in value.items():
                if not isinstance(tk, str) or not _TYPE_TOKEN.match(tk):
                    raise RollupError(
                        f"kg_nodes_by_type key {tk!r} is not a safe enum token "
                        "(free-text/PII guard)"
                    )
                if not isinstance(tv, int) or isinstance(tv, bool):
                    raise RollupError(f"kg_nodes_by_type[{tk!r}] must be an int")
        else:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise RollupError(f"{key!r} must be an int/float count, got {value!r}")
    return day


# ---------------------------------------------------------------------------
# Read-only DB access. Everything below is defensive: a missing table/column or
# a malformed value yields 0, never an exception into the caller.
# ---------------------------------------------------------------------------


def _connect_ro(db_path: str):  # -> sqlite3.Connection
    """Open ``db_path`` read-only (``mode=ro`` URI) with a short busy timeout.

    ``mode=ro`` means the rollup can never create, write, or migrate the DB — a
    non-existent path raises here (caught by the caller), and a busy writer is
    waited on for at most 2s before we give up and report zeros.
    """
    import sqlite3

    uri = f"file:{db_path}?mode=ro"
    return sqlite3.connect(uri, uri=True, timeout=2.0)


def _scalar(conn, sql: str, params: tuple = ()) -> int | float:
    """Run a single-value aggregate; any failure (missing table/col) → 0."""
    try:
        row = conn.execute(sql, params).fetchone()
    except Exception:  # noqa: BLE001 — partial/older schema must not break the rollup
        return 0
    if not row or row[0] is None:
        return 0
    return row[0]


def _kg_snapshot(conn, *, today: str) -> dict[str, Any]:
    """Current KG scale gauges (a 'now' snapshot; not reconstructable per past day).

    Attached only to the current day's row by :func:`compute_rollup`. ``kg_growth_7d``
    = count of non-deprecated nodes created in the 7-day window ending ``today``.
    """
    from datetime import date, timedelta

    nodes = int(_scalar(conn, "SELECT COUNT(*) FROM graph_nodes WHERE deprecated_at IS NULL"))
    edges = int(_scalar(conn, "SELECT COUNT(*) FROM graph_edges WHERE valid_until IS NULL"))
    cutoff = (date.fromisoformat(today) - timedelta(days=6)).isoformat()
    growth = int(
        _scalar(
            conn,
            "SELECT COUNT(*) FROM graph_nodes "
            "WHERE deprecated_at IS NULL AND substr(created_at,1,10) >= ?",
            (cutoff,),
        )
    )
    by_type: dict[str, int] = {}
    try:
        cur = conn.execute(
            "SELECT type, COUNT(*) FROM graph_nodes "
            "WHERE deprecated_at IS NULL GROUP BY type"
        )
        for t, c in cur.fetchall():
            if isinstance(t, str) and _TYPE_TOKEN.match(t):
                by_type[t] = int(c)
    except Exception:  # noqa: BLE001
        by_type = {}
    return {
        "kg_nodes": nodes,
        "kg_edges": edges,
        "kg_growth_7d": growth,
        "kg_nodes_by_type": by_type,
    }


def _day_row(conn, day: str) -> dict[str, Any]:
    """Compute the per-day time-series metrics for one UTC ``day`` (YYYY-MM-DD).

    ``substr(col,1,10)`` is used everywhere instead of ``date(col)`` so the match
    is bulletproof across the three timestamp shapes in console.db: space-separated
    (``sessions_meta``/``tasks``), and ISO-with-``Z`` (``audit_log``).
    """
    cost = _scalar(
        conn,
        "SELECT COALESCE(SUM(cost_usd),0) FROM session_costs "
        "WHERE substr(COALESCE(completed_at,updated_at),1,10) = ?",
        (day,),
    )
    active_seconds = _scalar(
        conn,
        "SELECT COALESCE(SUM(working_seconds),0) FROM sessions_meta "
        "WHERE substr(created_at,1,10) = ?",
        (day,),
    )
    return {
        "day": day,
        "sessions": int(
            _scalar(conn, "SELECT COUNT(*) FROM sessions_meta WHERE substr(created_at,1,10)=?", (day,))
        ),
        "active_min": int(active_seconds // 60),
        "cost_usd": round(float(cost), 6),
        "tokens_in": int(
            _scalar(
                conn,
                "SELECT COALESCE(SUM(input_tokens),0) FROM session_costs "
                "WHERE substr(COALESCE(completed_at,updated_at),1,10)=?",
                (day,),
            )
        ),
        "tokens_out": int(
            _scalar(
                conn,
                "SELECT COALESCE(SUM(output_tokens),0) FROM session_costs "
                "WHERE substr(COALESCE(completed_at,updated_at),1,10)=?",
                (day,),
            )
        ),
        "tasks_created": int(
            _scalar(
                conn,
                "SELECT COUNT(*) FROM tasks WHERE deleted_at IS NULL AND substr(created_at,1,10)=?",
                (day,),
            )
        ),
        "tasks_completed": int(
            _scalar(
                conn,
                "SELECT COUNT(*) FROM tasks WHERE status='completed' AND substr(updated_at,1,10)=?",
                (day,),
            )
        ),
        "searches": int(
            _scalar(
                conn,
                "SELECT COUNT(*) FROM audit_log WHERE resource_type='search' AND substr(timestamp,1,10)=?",
                (day,),
            )
        ),
        "mcp_calls": int(
            _scalar(
                conn,
                "SELECT COUNT(*) FROM audit_log WHERE action='tool_call' AND substr(timestamp,1,10)=?",
                (day,),
            )
        ),
    }


def compute_rollup(
    db_path: str, *, days_back: int = 7, today: str | None = None
) -> list[dict[str, Any]]:
    """Compute the last ``days_back`` daily rollups from ``db_path`` (newest first).

    Each element is a validated day payload (see :func:`validate_day`). The current
    day's row also carries the KG scale snapshot (:func:`_kg_snapshot`); past rows
    carry only the time-series metrics (KG totals are not reconstructable per day).

    Sending an array of recent days lets the server backfill days the client missed
    (offline / not run), upserting idempotently on ``(install_id, day)``.

    Read-only and defensive: a missing table yields zeros; only a bad ``db_path``
    (unopenable) raises — the caller (``sender``) swallows that, fail-silent.
    """
    from datetime import date, datetime, timedelta, timezone

    if today is None:
        today = datetime.now(timezone.utc).date().isoformat()
    base = date.fromisoformat(today)

    conn = _connect_ro(db_path)
    try:
        kg = _kg_snapshot(conn, today=today)
        out: list[dict[str, Any]] = []
        for i in range(max(1, days_back)):
            day = (base - timedelta(days=i)).isoformat()
            row = _day_row(conn, day)
            if i == 0:  # the current day carries the KG "now" snapshot
                row.update(kg)
            out.append(validate_day(row))
        return out
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
