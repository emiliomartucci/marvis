# Brain v1 — Events read API (sub-01 D6, NEW 2026-05-16).
# Consumed by HTTP GET /api/v1/brain/events, the Console PipelineSubbar Digest
# station, and the EvidenceDrawer drill-down. Stable cursor sort
# (observed_at DESC, event_id) per parent §9.8.
from __future__ import annotations

import base64
import binascii
import json
import logging
from datetime import datetime, timezone
from typing import Any

import aiosqlite

from core.api.db import acquire_db
from core.api.models import UserInfo
from core.api.models.brain import (
    DigestEvent,
    DigestEventRedacted,
    EventsListResponse,
    EventType,
)
from core.api.visibility import get_visible_projects

logger = logging.getLogger(__name__)

MAX_LIMIT = 200
DEFAULT_LIMIT = 50


def _encode_cursor(observed_at: str, event_id: str) -> str:
    payload = json.dumps({"o": observed_at, "e": event_id}, separators=(",", ":"))
    return base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii")


def _decode_cursor(cursor: str) -> tuple[str, str] | None:
    try:
        raw = base64.urlsafe_b64decode(cursor.encode("ascii"))
        payload = json.loads(raw)
        return payload["o"], payload["e"]
    except (KeyError, ValueError, binascii.Error, json.JSONDecodeError, UnicodeDecodeError):
        return None


def _parse_iso(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _is_visible(
    visible: set[str] | None, source_project: str | None, target_project: str | None
) -> bool:
    if visible is None:
        return True
    projects = {p for p in (source_project, target_project) if p}
    if not projects:
        return True
    return projects.issubset(visible)


async def _resolve_run(
    db: aiosqlite.Connection,
    *,
    cycle_key: str | None,
    run_id: str | None,
    workspace_id: str,
) -> dict[str, Any] | None:
    if run_id:
        row = await (
            await db.execute(
                "SELECT run_id, cycle_key FROM brain_runs WHERE run_id = ?",
                (run_id,),
            )
        ).fetchone()
        if row is None:
            return None
        return {"run_id": row[0], "cycle_key": row[1]}

    if not cycle_key or cycle_key == "latest":
        row = await (
            await db.execute(
                "SELECT run_id, cycle_key FROM brain_runs "
                "WHERE workspace_id = ? AND status IN ('succeeded', 'partial') "
                "AND superseded_by_run_id IS NULL "
                "ORDER BY cycle_key DESC, started_at DESC LIMIT 1",
                (workspace_id,),
            )
        ).fetchone()
        if row is None:
            return None
        return {"run_id": row[0], "cycle_key": row[1]}

    row = await (
        await db.execute(
            "SELECT run_id, cycle_key FROM brain_runs "
            "WHERE workspace_id = ? AND cycle_key = ? "
            "AND status IN ('succeeded', 'partial') "
            "AND superseded_by_run_id IS NULL "
            "ORDER BY started_at DESC LIMIT 1",
            (workspace_id, cycle_key),
        )
    ).fetchone()
    if row is None:
        return None
    return {"run_id": row[0], "cycle_key": row[1]}


async def list_events_for_cycle(
    *,
    cycle_key: str | None = None,
    run_id: str | None = None,
    event_type: list[EventType] | None = None,
    source_project: str | None = None,
    cursor: str | None = None,
    limit: int = DEFAULT_LIMIT,
    user: UserInfo | None = None,
    workspace_id: str = "ws_default",
) -> EventsListResponse:
    """Return paginated events with visibility filter applied."""
    limit = max(1, min(MAX_LIMIT, int(limit)))
    over_fetch = limit + 1

    async with acquire_db() as db:
        db.row_factory = aiosqlite.Row
        run = await _resolve_run(
            db, cycle_key=cycle_key, run_id=run_id, workspace_id=workspace_id
        )
        if run is None:
            return EventsListResponse(items=[], next_cursor=None, total_returned=0)

        visible = await get_visible_projects(db, user, workspace_id) if user else None

        where = ["e.run_id = ?"]
        params: list[Any] = [run["run_id"]]

        if event_type:
            placeholders = ",".join("?" for _ in event_type)
            where.append(f"e.event_type IN ({placeholders})")
            params.extend(event_type)

        if source_project:
            where.append("(e.source_project = ? OR e.target_project = ?)")
            params.extend([source_project, source_project])

        if cursor:
            decoded = _decode_cursor(cursor)
            if decoded:
                observed_after, last_event_id = decoded
                where.append("(e.observed_at < ? OR (e.observed_at = ? AND e.event_id > ?))")
                params.extend([observed_after, observed_after, last_event_id])

        query = (
            "SELECT event_id, run_id, cycle_key, observed_at, derived_from_state_at, "
            "       event_type, schema_version, source_system, source_project, "
            "       target_project, program_key, source_ref, title, summary, "
            "       evidence_json, evidence_hash "
            "FROM brain_digest_events e "
            f"WHERE {' AND '.join(where)} "
            "ORDER BY e.observed_at DESC, e.event_id ASC LIMIT ?"
        )
        params.append(over_fetch)

        rows = await (await db.execute(query, params)).fetchall()

    items: list[DigestEvent | DigestEventRedacted] = []
    next_cursor: str | None = None
    redacted_count = 0
    page_rows = rows[:limit]
    if len(rows) > limit:
        last = page_rows[-1]
        next_cursor = _encode_cursor(last["observed_at"], last["event_id"])

    for row in page_rows:
        if not _is_visible(visible, row["source_project"], row["target_project"]):
            redacted_count += 1
            items.append(
                DigestEventRedacted(
                    event_id=row["event_id"],
                    cycle_key=row["cycle_key"],
                    event_type=row["event_type"],
                )
            )
            continue
        try:
            evidence = json.loads(row["evidence_json"] or "{}")
        except json.JSONDecodeError:
            evidence = {}
        items.append(
            DigestEvent(
                event_id=row["event_id"],
                run_id=row["run_id"],
                cycle_key=row["cycle_key"],
                observed_at=_parse_iso(row["observed_at"]),
                derived_from_state_at=_parse_iso(row["derived_from_state_at"]),
                event_type=row["event_type"],
                schema_version=row["schema_version"],
                source_system=row["source_system"],
                source_project=row["source_project"],
                target_project=row["target_project"],
                program_key=row["program_key"],
                source_ref=row["source_ref"],
                title=row["title"],
                summary=row["summary"] or "",
                evidence=evidence,
                evidence_hash=row["evidence_hash"],
            )
        )

    return EventsListResponse(
        items=items,
        next_cursor=next_cursor,
        cycle_key=run["cycle_key"],
        run_id=run["run_id"],
        redacted_count=redacted_count,
        total_returned=len(items),
    )


__all__ = ["list_events_for_cycle", "MAX_LIMIT", "DEFAULT_LIMIT"]
