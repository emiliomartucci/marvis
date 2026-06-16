# Brain v1 — Journal read API (sub-01 §5.3).
# Downstream layers (Drift, Memory Operations, Learn) MUST call these helpers
# instead of running raw SQL on brain_journal_entries — layering invariant.
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from core.api.db import acquire_db
from core.api.models.brain import JournalBody, JournalEntry, ScopeType


def _parse_iso(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _row_to_entry(row: Any) -> JournalEntry:
    body_raw = row["body_json"] if hasattr(row, "keys") else row[7]
    try:
        body_payload = json.loads(body_raw or "{}")
    except json.JSONDecodeError:
        body_payload = {}
    # Wave 3.1 gap 2: persistent narrative_polished read from DB column. The
    # in-memory router_glue cache still wins on cache hit; the persisted value
    # is the baseline so historical journals (Console "Giornale ultimi 30
    # giorni") never miss the polish.
    narrative_polished = None
    polish_model = None
    if hasattr(row, "keys"):
        keys = set(row.keys())
        if "narrative_polished" in keys:
            narrative_polished = row["narrative_polished"]
        if "narrative_polished_model" in keys:
            polish_model = row["narrative_polished_model"]
    entry_kwargs: dict[str, Any] = {
        "entry_id": row[0] if not hasattr(row, "keys") else row["entry_id"],
        "run_id": row[1] if not hasattr(row, "keys") else row["run_id"],
        "workspace_id": row[2] if not hasattr(row, "keys") else row["workspace_id"],
        "cycle_key": row[3] if not hasattr(row, "keys") else row["cycle_key"],
        "scope_type": (row[4] if not hasattr(row, "keys") else row["scope_type"]),
        "scope_key": row[5] if not hasattr(row, "keys") else row["scope_key"],
        "program_key": row[6] if not hasattr(row, "keys") else row["program_key"],
        "body": JournalBody(**body_payload),
        "is_empty": bool(row[8] if not hasattr(row, "keys") else row["is_empty"]),
        "published_at": _parse_iso(
            row[9] if not hasattr(row, "keys") else row["published_at"]
        ),
    }
    if narrative_polished:
        entry_kwargs["narrative_polished"] = narrative_polished
    if polish_model:
        entry_kwargs["polish_model"] = polish_model
    return JournalEntry(**entry_kwargs)


_SELECT_COLUMNS = (
    "entry_id, run_id, workspace_id, cycle_key, scope_type, scope_key, "
    "program_key, body_json, is_empty, published_at, narrative_polished, "
    "narrative_polished_at, narrative_polished_model"
)


async def get_latest_entry(
    scope_type: ScopeType,
    scope_key: str,
    *,
    before: str | None = None,
    workspace_id: str = "ws_default",
) -> JournalEntry | None:
    """Return the most recent published entry for the given scope.

    Restricts to runs that succeeded fully or partially — superseded/failed
    runs never surface here (sub-01 §10 invariant #4).
    """
    where = [
        "j.workspace_id = ?",
        "j.scope_type = ?",
        "j.scope_key = ?",
        "r.status IN ('succeeded', 'partial')",
        "r.superseded_by_run_id IS NULL",
    ]
    params: list[Any] = [workspace_id, scope_type, scope_key]
    if before:
        where.append("j.cycle_key < ?")
        params.append(before)
    query = (
        f"SELECT {', '.join('j.' + c for c in _SELECT_COLUMNS.split(', '))} "
        f"FROM brain_journal_entries j "
        f"JOIN brain_runs r ON r.run_id = j.run_id "
        f"WHERE {' AND '.join(where)} "
        f"ORDER BY j.cycle_key DESC LIMIT 1"
    )
    async with acquire_db() as db:
        db.row_factory = __import__("aiosqlite").Row
        row = await (await db.execute(query, params)).fetchone()
    return _row_to_entry(row) if row else None


async def list_entries(
    scope_type: ScopeType,
    scope_key: str,
    *,
    since: str,
    until: str,
    workspace_id: str = "ws_default",
) -> list[JournalEntry]:
    """Return entries in [since, until] inclusive, newest first."""
    query = (
        f"SELECT {', '.join('j.' + c for c in _SELECT_COLUMNS.split(', '))} "
        f"FROM brain_journal_entries j "
        f"JOIN brain_runs r ON r.run_id = j.run_id "
        f"WHERE j.workspace_id = ? AND j.scope_type = ? AND j.scope_key = ? "
        f"AND j.cycle_key >= ? AND j.cycle_key <= ? "
        f"AND r.status IN ('succeeded', 'partial') "
        f"AND r.superseded_by_run_id IS NULL "
        f"ORDER BY j.cycle_key DESC"
    )
    async with acquire_db() as db:
        db.row_factory = __import__("aiosqlite").Row
        rows = await (
            await db.execute(
                query, (workspace_id, scope_type, scope_key, since, until)
            )
        ).fetchall()
    return [_row_to_entry(r) for r in rows]


async def list_entries_for_cycle(
    *,
    cycle_key: str | None,
    run_id: str | None,
    scope_type: str | None,
    scope_key: str | None,
    program_key: str | None,
    workspace_id: str = "ws_default",
    limit: int = 50,
) -> list[JournalEntry]:
    """Surface listing for sub-05 §2 `/api/v1/brain/journal`.

    Resolves `cycle_key='latest'` to the most recent succeeded cycle. Filters
    by scope_type/scope_key/program_key when provided. Stable sort:
    `(cycle_key DESC, scope_type ASC, scope_key ASC)`.
    """
    where = [
        "j.workspace_id = ?",
        "r.status IN ('succeeded', 'partial')",
        "r.superseded_by_run_id IS NULL",
    ]
    params: list[Any] = [workspace_id]

    resolved_cycle: str | None = None
    if cycle_key == "latest":
        async with acquire_db() as db:
            db.row_factory = __import__("aiosqlite").Row
            async with db.execute(
                "SELECT cycle_key FROM brain_runs "
                "WHERE workspace_id = ? AND status = 'succeeded' "
                "AND superseded_by_run_id IS NULL "
                "ORDER BY cycle_key DESC LIMIT 1",
                (workspace_id,),
            ) as cur:
                row = await cur.fetchone()
        if row:
            resolved_cycle = row["cycle_key"]
        else:
            return []
        where.append("j.cycle_key = ?")
        params.append(resolved_cycle)
    elif cycle_key:
        where.append("j.cycle_key = ?")
        params.append(cycle_key)

    if run_id:
        where.append("j.run_id = ?")
        params.append(run_id)
    if scope_type:
        where.append("j.scope_type = ?")
        params.append(scope_type)
    if scope_key:
        where.append("j.scope_key = ?")
        params.append(scope_key)
    if program_key:
        where.append("j.program_key = ?")
        params.append(program_key)

    query = (
        f"SELECT {', '.join('j.' + c for c in _SELECT_COLUMNS.split(', '))} "
        f"FROM brain_journal_entries j "
        f"JOIN brain_runs r ON r.run_id = j.run_id "
        f"WHERE {' AND '.join(where)} "
        f"ORDER BY j.cycle_key DESC, j.scope_type ASC, j.scope_key ASC "
        f"LIMIT ?"
    )
    params.append(int(limit))

    async with acquire_db() as db:
        db.row_factory = __import__("aiosqlite").Row
        rows = await (await db.execute(query, params)).fetchall()
    return [_row_to_entry(r) for r in rows]


__all__ = ["get_latest_entry", "list_entries", "list_entries_for_cycle"]
