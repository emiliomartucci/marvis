# brain_source_watermarks read/write helpers (D0).
# Each source maintains its own watermark to avoid re-scanning history every cycle.
from __future__ import annotations

from datetime import datetime, timezone

import aiosqlite

from core.api.db import acquire_db, write_db


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _epoch_utc() -> datetime:
    return datetime(1970, 1, 1, tzinfo=timezone.utc)


async def _fetch_watermark(
    db: aiosqlite.Connection, *, source_system: str, workspace_id: str
) -> datetime:
    row = await (
        await db.execute(
            "SELECT last_observed_at FROM brain_source_watermarks "
            "WHERE source_system = ? AND workspace_id = ?",
            (source_system, workspace_id),
        )
    ).fetchone()
    if row is None:
        return _epoch_utc()
    raw = row[0] if not hasattr(row, "keys") else row["last_observed_at"]
    return _parse_iso(raw) or _epoch_utc()


async def get_watermark(
    *, source_system: str, workspace_id: str = "ws_default"
) -> datetime:
    """Return last_observed_at for the source, epoch if absent."""
    async with acquire_db() as db:
        return await _fetch_watermark(
            db, source_system=source_system, workspace_id=workspace_id
        )


async def reset_watermarks(
    *,
    workspace_id: str,
    to_iso: str,
    source_systems: list[str] | None = None,
    now: datetime,
) -> list[dict[str, str]]:
    """Force `last_observed_at` of selected source watermarks to a past instant.

    Use case: monthly backfill — watermarks advance one-way, blocking
    historical cycles from re-reading the substrate. Reset to a past date
    so subsequent cycles see events again.

    Returns the list of {source_system, previous_observed_at, new_observed_at}
    so the caller can audit the rollback.
    """
    target = _parse_iso(to_iso)
    if target is None:
        raise ValueError(f"to_iso must be ISO 8601 (got: {to_iso!r})")
    target_value = target.astimezone(timezone.utc).isoformat()
    now_value = now.astimezone(timezone.utc).isoformat()

    audit: list[dict[str, str]] = []
    async with write_db() as db:
        if source_systems is None:
            async with db.execute(
                "SELECT source_system FROM brain_source_watermarks "
                "WHERE workspace_id = ?",
                (workspace_id,),
            ) as cur:
                rows = await cur.fetchall()
            source_systems = [
                (r[0] if not hasattr(r, "keys") else r["source_system"]) for r in rows
            ]
        for src in source_systems:
            current = await _fetch_watermark(
                db, source_system=src, workspace_id=workspace_id
            )
            await db.execute(
                "INSERT INTO brain_source_watermarks "
                "(source_system, workspace_id, last_observed_at, last_event_id, last_cycle_key, updated_at) "
                "VALUES (?, ?, ?, NULL, NULL, ?) "
                "ON CONFLICT(source_system, workspace_id) DO UPDATE SET "
                "  last_observed_at = excluded.last_observed_at, "
                "  last_event_id    = NULL, "
                "  last_cycle_key   = NULL, "
                "  updated_at       = excluded.updated_at",
                (src, workspace_id, target_value, now_value),
            )
            audit.append(
                {
                    "source_system": src,
                    "previous_observed_at": current.isoformat(),
                    "new_observed_at": target_value,
                }
            )
    return audit


async def advance_watermark(
    *,
    source_system: str,
    workspace_id: str,
    observed_at: datetime,
    last_event_id: str | None,
    cycle_key: str,
    now: datetime,
) -> None:
    """Upsert watermark to a later observed_at. Idempotent: never moves backwards."""
    value = observed_at.astimezone(timezone.utc).isoformat()
    async with write_db() as db:
        current = await _fetch_watermark(
            db, source_system=source_system, workspace_id=workspace_id
        )
        if observed_at <= current:
            return
        await db.execute(
            "INSERT INTO brain_source_watermarks "
            "(source_system, workspace_id, last_observed_at, last_event_id, last_cycle_key, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(source_system, workspace_id) DO UPDATE SET "
            "  last_observed_at = excluded.last_observed_at, "
            "  last_event_id    = excluded.last_event_id, "
            "  last_cycle_key   = excluded.last_cycle_key, "
            "  updated_at       = excluded.updated_at",
            (
                source_system,
                workspace_id,
                value,
                last_event_id,
                cycle_key,
                now.astimezone(timezone.utc).isoformat(),
            ),
        )
