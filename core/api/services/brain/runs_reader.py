# Brain v1 — Runs read API (sub-05 §2 + sub-01 §6.D4).
# Pulls envelope rows from brain_runs and projects them to the BrainRun
# Pydantic contract. Cursor pagination + visibility-passthrough.
from __future__ import annotations

import base64
import json
from datetime import datetime, timezone
from typing import Any

import aiosqlite

from core.api.db import acquire_db, write_db
from core.api.models.brain import BrainRun, PipelineCounters, RunsListResponse, RunStatus

DEFAULT_LIMIT = 50
MAX_LIMIT = 200

_VALID_STATUSES: tuple[str, ...] = (
    "running",
    "succeeded",
    "partial",
    "failed",
    "superseded",
)


def _parse_iso(value: str | None) -> datetime | None:
    if value is None:
        return None
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _encode_cursor(cycle_key: str, run_id: str) -> str:
    payload = json.dumps({"c": cycle_key, "r": run_id}, separators=(",", ":"))
    return base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii")


def _decode_cursor(cursor: str | None) -> tuple[str, str] | None:
    if not cursor:
        return None
    try:
        raw = base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8")
        parsed = json.loads(raw)
        return str(parsed["c"]), str(parsed["r"])
    except (ValueError, KeyError, json.JSONDecodeError):
        return None


def _row_to_run(row: aiosqlite.Row) -> BrainRun:
    failures_raw = row["partial_failures_json"] if "partial_failures_json" in row.keys() else "[]"
    try:
        partial = json.loads(failures_raw or "[]")
    except json.JSONDecodeError:
        partial = []
    return BrainRun(
        run_id=row["run_id"],
        workspace_id=row["workspace_id"],
        cycle_key=row["cycle_key"],
        cycle_window_start_utc=_parse_iso(row["cycle_window_start_utc"]),
        cycle_window_end_utc=_parse_iso(row["cycle_window_end_utc"]),
        cutoff_hour_utc_at_run=int(row["cutoff_hour_utc_at_run"] or 0),
        scope_type="company",
        scope_key=row["scope_key"] or "__company__",
        trigger=row["trigger"],
        triggered_by=row["triggered_by"],
        started_at=_parse_iso(row["started_at"]),
        finished_at=_parse_iso(row["finished_at"]),
        status=row["status"],
        superseded_by_run_id=row["superseded_by_run_id"],
        event_count=int(row["event_count"] or 0),
        partial_failures=partial,
        duration_ms=row["duration_ms"],
        error_summary=row["error_summary"],
    )


async def _resolve_latest_cycle_key(
    db: aiosqlite.Connection, *, workspace_id: str
) -> str | None:
    async with db.execute(
        "SELECT cycle_key FROM brain_runs "
        "WHERE workspace_id = ? AND status = 'succeeded' "
        "AND superseded_by_run_id IS NULL "
        "ORDER BY cycle_key DESC LIMIT 1",
        (workspace_id,),
    ) as cur:
        row = await cur.fetchone()
    return row["cycle_key"] if row else None


async def list_runs(
    *,
    cycle_key: str | None,
    status: list[str] | None,
    trigger: list[str] | None,
    include_superseded: bool,
    cursor: str | None,
    limit: int,
    workspace_id: str = "ws_default",
) -> RunsListResponse:
    """Stable sort: (cycle_key DESC, run_id ASC)."""
    where = ["workspace_id = ?"]
    params: list[Any] = [workspace_id]
    resolved_cycle: str | None = None

    if cycle_key == "latest":
        async with acquire_db() as db:
            db.row_factory = aiosqlite.Row
            resolved_cycle = await _resolve_latest_cycle_key(
                db, workspace_id=workspace_id
            )
        if resolved_cycle:
            where.append("cycle_key = ?")
            params.append(resolved_cycle)
        else:
            return RunsListResponse(
                items=[], next_cursor=None, cycle_key=None, total_returned=0
            )
    elif cycle_key:
        where.append("cycle_key = ?")
        params.append(cycle_key)
        resolved_cycle = cycle_key

    if status:
        valid = [s for s in status if s in _VALID_STATUSES]
        if valid:
            placeholders = ",".join("?" for _ in valid)
            where.append(f"status IN ({placeholders})")
            params.extend(valid)

    if trigger:
        placeholders = ",".join("?" for _ in trigger)
        where.append(f"trigger IN ({placeholders})")
        params.extend(trigger)

    if not include_superseded:
        where.append("superseded_by_run_id IS NULL")

    decoded = _decode_cursor(cursor)
    if decoded:
        cur_cycle, cur_run = decoded
        where.append("(cycle_key < ? OR (cycle_key = ? AND run_id > ?))")
        params.extend([cur_cycle, cur_cycle, cur_run])

    fetch_limit = min(max(limit, 1), MAX_LIMIT) + 1
    query = (
        "SELECT run_id, workspace_id, cycle_key, "
        "cycle_window_start_utc, cycle_window_end_utc, cutoff_hour_utc_at_run, "
        "scope_type, scope_key, trigger, triggered_by, started_at, finished_at, "
        "status, superseded_by_run_id, event_count, partial_failures_json, "
        "duration_ms, error_summary "
        "FROM brain_runs WHERE " + " AND ".join(where) +
        " ORDER BY cycle_key DESC, run_id ASC LIMIT ?"
    )
    params.append(fetch_limit)

    async with acquire_db() as db:
        db.row_factory = aiosqlite.Row
        rows = list(await (await db.execute(query, params)).fetchall())

    next_cursor: str | None = None
    if len(rows) > limit:
        last = rows[limit - 1]
        next_cursor = _encode_cursor(last["cycle_key"], last["run_id"])
        rows = rows[:limit]

    items = [_row_to_run(r) for r in rows]
    return RunsListResponse(
        items=items,
        next_cursor=next_cursor,
        cycle_key=resolved_cycle,
        total_returned=len(items),
    )


class PromoteError(Exception):
    """Raised when a promote_run pre-condition fails (404 / 422 surface)."""

    def __init__(self, kind: str, detail: str):
        self.kind = kind
        self.detail = detail
        super().__init__(detail)


async def promote_run(
    run_id: str, *, workspace_id: str = "ws_default"
) -> BrainRun:
    """Revert a supersede so a previously-replaced run becomes UX-visible again.

    Use case: a low-yield manual recompute superseded a richer auto-cron run.
    The richer run still holds its journal/drift/memory_op/finding rows but
    `superseded_by_run_id` keeps them hidden behind the visibility filter
    (`r.superseded_by_run_id IS NULL`) on every read path.

    Effects:
      1. Validate target run exists in workspace AND is currently superseded.
      2. Clear `superseded_by_run_id` on target → it surfaces again.
      3. Cascade-clear: the successor that pointed at target loses its
         `superseded_by_run_id` reverse-pointer entry only if it itself was
         the chain head (no further superseder). This keeps the chain
         well-formed: at most one head per cycle is UX-visible.

    Raises `PromoteError` with kind:
      - 'not_found': run_id unknown in workspace.
      - 'not_superseded': target run is already UX-visible (idempotency).
    """
    # Read pre-conditions on the readonly pool (fast path; no writer lock).
    async with acquire_db() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT run_id, status, superseded_by_run_id, cycle_key "
            "FROM brain_runs WHERE run_id = ? AND workspace_id = ?",
            (run_id, workspace_id),
        ) as cur:
            target = await cur.fetchone()
    if target is None:
        raise PromoteError("not_found", f"run {run_id} not found")
    successor_run_id = target["superseded_by_run_id"]
    if successor_run_id is None:
        raise PromoteError(
            "not_superseded",
            f"run {run_id} is already the visible head (no superseder to revert)",
        )

    # SWAP heads on the writer connection (single-writer lock).
    # Order matters: the partial unique index
    #   uniq_brain_runs_active_cycle ON (workspace_id, cycle_key)
    #     WHERE status IN ('running','succeeded') AND superseded_by_run_id IS NULL
    # caps the cycle at one "head" row. If we mark target as head BEFORE
    # demoting successor, both rows transiently match the index → IntegrityError.
    # Demote successor FIRST (it drops out of the index because status moves
    # to 'superseded'), THEN promote target (it joins the index in successor's
    # place). At most one head per cycle at any point during the txn.
    async with write_db() as db:
        db.row_factory = aiosqlite.Row
        # Step 1 — demote successor: status -> 'superseded' + point at target.
        await db.execute(
            "UPDATE brain_runs SET superseded_by_run_id = ?, "
            "status = CASE WHEN status = 'succeeded' THEN 'superseded' ELSE status END "
            "WHERE run_id = ? AND workspace_id = ?",
            (run_id, successor_run_id, workspace_id),
        )
        # Step 2 — promote target: clear pointer + status -> 'succeeded'.
        await db.execute(
            "UPDATE brain_runs SET superseded_by_run_id = NULL, "
            "status = CASE WHEN status = 'superseded' THEN 'succeeded' ELSE status END "
            "WHERE run_id = ? AND workspace_id = ?",
            (run_id, workspace_id),
        )

        # Re-read the promoted run for response inside the same writer txn
        # so we observe the post-update state.
        async with db.execute(
            "SELECT run_id, workspace_id, cycle_key, "
            "cycle_window_start_utc, cycle_window_end_utc, cutoff_hour_utc_at_run, "
            "scope_type, scope_key, trigger, triggered_by, started_at, finished_at, "
            "status, superseded_by_run_id, event_count, partial_failures_json, "
            "duration_ms, error_summary "
            "FROM brain_runs WHERE run_id = ? AND workspace_id = ?",
            (run_id, workspace_id),
        ) as cur:
            row = await cur.fetchone()
    return _row_to_run(row)


class DiscardError(Exception):
    """Raised when a discard_run pre-condition fails (404 / 422 surface)."""

    def __init__(self, kind: str, detail: str):
        self.kind = kind
        self.detail = detail
        super().__init__(detail)


async def discard_run(
    run_id: str, *, workspace_id: str = "ws_default"
) -> BrainRun:
    """Mark a head run as 'failed' so it drops out of UX `latest` resolution.

    Use case: a low-yield manual recompute creates a head run (e.g. cycle
    forward 2026-05-18 with 1 event) that intercepts `cycle_key=latest`
    resolution. Discarding the head removes it from the active-cycle
    partial unique index (which only matches `status IN
    ('running','succeeded') AND superseded_by_run_id IS NULL`), so the
    next visible cycle (the richer one one day prior) wins `latest`.

    Effects:
      1. Validate target run exists, is the head (superseded_by_run_id NULL),
         and is currently in a discardable status (succeeded/partial).
      2. Set status = 'failed' (preserves audit trail vs. delete; row stays
         queryable with `include_superseded=true` for forensics).

    Raises `DiscardError` with kind:
      - 'not_found': run_id unknown in workspace.
      - 'not_head': run is already superseded (won't surface anyway).
      - 'not_discardable': status is already terminal-non-success.
    """
    async with acquire_db() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT run_id, status, superseded_by_run_id, cycle_key "
            "FROM brain_runs WHERE run_id = ? AND workspace_id = ?",
            (run_id, workspace_id),
        ) as cur:
            target = await cur.fetchone()
    if target is None:
        raise DiscardError("not_found", f"run {run_id} not found")
    if target["superseded_by_run_id"] is not None:
        raise DiscardError(
            "not_head",
            f"run {run_id} is already superseded — won't surface in UX latest",
        )
    if target["status"] not in ("succeeded", "partial"):
        raise DiscardError(
            "not_discardable",
            f"run {run_id} has status='{target['status']}' (only succeeded/partial discardable)",
        )

    async with write_db() as db:
        db.row_factory = aiosqlite.Row
        await db.execute(
            "UPDATE brain_runs SET status = 'failed' "
            "WHERE run_id = ? AND workspace_id = ?",
            (run_id, workspace_id),
        )
        async with db.execute(
            "SELECT run_id, workspace_id, cycle_key, "
            "cycle_window_start_utc, cycle_window_end_utc, cutoff_hour_utc_at_run, "
            "scope_type, scope_key, trigger, triggered_by, started_at, finished_at, "
            "status, superseded_by_run_id, event_count, partial_failures_json, "
            "duration_ms, error_summary "
            "FROM brain_runs WHERE run_id = ? AND workspace_id = ?",
            (run_id, workspace_id),
        ) as cur:
            row = await cur.fetchone()
    return _row_to_run(row)


async def fetch_single_run(
    run_id: str, *, workspace_id: str = "ws_default"
) -> BrainRun | None:
    async with acquire_db() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT run_id, workspace_id, cycle_key, "
            "cycle_window_start_utc, cycle_window_end_utc, cutoff_hour_utc_at_run, "
            "scope_type, scope_key, trigger, triggered_by, started_at, finished_at, "
            "status, superseded_by_run_id, event_count, partial_failures_json, "
            "duration_ms, error_summary "
            "FROM brain_runs WHERE run_id = ? AND workspace_id = ?",
            (run_id, workspace_id),
        ) as cur:
            row = await cur.fetchone()
    return _row_to_run(row) if row else None


async def get_pipeline_counters(
    *, cycle_key: str | None, workspace_id: str = "ws_default"
) -> PipelineCounters:
    """Aggregate 6-station counts for the PipelineSubbar.

    Resolves `latest` server-side. Counts are unfiltered (workspace-scoped) —
    visibility is applied client-side via redacted_count on downstream calls.
    The subbar is a status indicator, not a privileged surface.
    """
    async with acquire_db() as db:
        db.row_factory = aiosqlite.Row

        resolved_cycle = cycle_key
        if cycle_key == "latest" or cycle_key is None:
            resolved_cycle = await _resolve_latest_cycle_key(
                db, workspace_id=workspace_id
            )

        if not resolved_cycle:
            return PipelineCounters(
                cycle_key=cycle_key or "latest",
                run_id=None,
            )

        async with db.execute(
            "SELECT run_id, event_count FROM brain_runs "
            "WHERE workspace_id = ? AND cycle_key = ? "
            "AND status IN ('succeeded', 'partial') "
            "AND superseded_by_run_id IS NULL "
            "ORDER BY started_at DESC LIMIT 1",
            (workspace_id, resolved_cycle),
        ) as cur:
            run_row = await cur.fetchone()

        run_id = run_row["run_id"] if run_row else None
        digest_count = int(run_row["event_count"]) if run_row else 0

        # Journal: count entries for this run
        journal_count = 0
        if run_id:
            async with db.execute(
                "SELECT COUNT(*) AS c FROM brain_journal_entries "
                "WHERE workspace_id = ? AND run_id = ?",
                (workspace_id, run_id),
            ) as cur:
                row = await cur.fetchone()
                journal_count = int(row["c"]) if row else 0

        # Drift: count signals in 'open' state for this cycle.
        # brain_drift_signals does NOT have a workspace_id column — visibility
        # is enforced on read paths via join to brain_runs. For the counter
        # (status indicator, not privileged surface — see docstring above)
        # cycle_key alone is sufficient.
        drift_count = 0
        try:
            async with db.execute(
                "SELECT COUNT(*) AS c FROM brain_drift_signals "
                "WHERE cycle_key = ? AND state = 'open'",
                (resolved_cycle,),
            ) as cur:
                row = await cur.fetchone()
                drift_count = int(row["c"]) if row else 0
        except aiosqlite.OperationalError:
            drift_count = 0

        # Memory ops: count pending operations (same rationale as drift).
        mem_count = 0
        try:
            async with db.execute(
                "SELECT COUNT(*) AS c FROM brain_memory_operations "
                "WHERE cycle_key = ? AND approval_state = 'pending'",
                (resolved_cycle,),
            ) as cur:
                row = await cur.fetchone()
                mem_count = int(row["c"]) if row else 0
        except aiosqlite.OperationalError:
            mem_count = 0

        # Findings: count open findings for this cycle (same rationale).
        findings_count = 0
        try:
            async with db.execute(
                "SELECT COUNT(*) AS c FROM brain_findings "
                "WHERE cycle_key = ? AND approval_state = 'open'",
                (resolved_cycle,),
            ) as cur:
                row = await cur.fetchone()
                findings_count = int(row["c"]) if row else 0
        except aiosqlite.OperationalError:
            findings_count = 0

        # Ingest: count pending ingest items (system-wide, no cycle scope)
        ingest_count = 0
        try:
            async with db.execute(
                "SELECT COUNT(*) AS c FROM ingest_pending WHERE status = 'pending'",
            ) as cur:
                row = await cur.fetchone()
                ingest_count = int(row["c"]) if row else 0
        except aiosqlite.OperationalError:
            ingest_count = 0

    return PipelineCounters(
        cycle_key=resolved_cycle,
        run_id=run_id,
        ingest=ingest_count,
        digest=digest_count,
        journal=journal_count,
        drift=drift_count,
        memory_ops=mem_count,
        findings=findings_count,
    )


def _all_run_statuses() -> tuple[str, ...]:
    return _VALID_STATUSES


__all__ = [
    "DEFAULT_LIMIT",
    "MAX_LIMIT",
    "fetch_single_run",
    "get_pipeline_counters",
    "list_runs",
]
