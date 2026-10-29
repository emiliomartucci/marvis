"""Shared default selection for readable Brain cycles."""
from __future__ import annotations

import aiosqlite

READABLE_STATUSES = ("succeeded", "partial")


async def latest_readable_run(
    db: aiosqlite.Connection, *, workspace_id: str,
    statuses: list[str] | None = None,
) -> dict[str, str] | None:
    selected = [s for s in READABLE_STATUSES if not statuses or s in statuses]
    if not selected:
        return None
    placeholders = ",".join("?" for _ in selected)
    row = await (await db.execute(
        "SELECT run_id, cycle_key FROM brain_runs WHERE workspace_id = ? "
        f"AND status IN ({placeholders}) AND superseded_by_run_id IS NULL "
        "ORDER BY cycle_key DESC, started_at DESC, run_id DESC LIMIT 1",
        (workspace_id, *selected),
    )).fetchone()
    return {"run_id": row[0], "cycle_key": row[1]} if row else None
