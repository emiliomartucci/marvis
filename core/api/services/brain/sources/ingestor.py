# Ingestor source collector (sub-01 §3 — `ingest_changed` + `external_update_seen`).
# Reads inbox_items rows whose updated_at falls in (watermark, cutoff_at]
# and yields one event per row. No business state is mutated.
from __future__ import annotations

import hashlib
import logging
from collections.abc import AsyncIterator

from core.api.db import acquire_db
from core.api.services.brain.digest_collector import SourceCollector
from core.api.services.brain.models import EventDraft, SourceCollectorContext
from core.api.services.brain.sources.base import (
    normalize_iso,
    resolve_source_project,
)

logger = logging.getLogger(__name__)


# Inbox sources that represent EXTERNAL feeds (RSS, email, web). These map to
# `external_update_seen` per plan §3 so the Drift layer can distinguish "user
# uploaded something" from "we noticed an upstream feed changed". Anything
# else (manual upload, API ingest, gmail-marvisx) maps to `ingest_changed`.
_EXTERNAL_SOURCES: frozenset[str] = frozenset({"rss", "email", "web", "external"})


def _classify_event_type(source_value: str | None) -> str:
    if not source_value:
        return "ingest_changed"
    return (
        "external_update_seen"
        if source_value.lower() in _EXTERNAL_SOURCES
        else "ingest_changed"
    )


def _source_ref(row_id: str, source_value: str | None, url: str | None) -> str:
    if (source_value or "").lower() in _EXTERNAL_SOURCES and url:
        url_hash = hashlib.blake2b(url.encode("utf-8"), digest_size=8).hexdigest()
        return f"external:{url_hash}"
    return f"ingest:{row_id}"


# Pagination page size — bounded read avoids loading >cap rows at once and
# enables collectors to span sources with >1000 events per cycle (bug fix
# 2026-05-18: previous code fetched a single LIMIT-less batch and the
# orchestrator-level cap silently truncated to per_source_event_cap).
_PAGE_SIZE: int = 500


async def collect(ctx: SourceCollectorContext) -> AsyncIterator[EventDraft]:
    """Yield ingestor events within the cycle window.

    Window = (max(watermark, cycle_window_start), min(cutoff_at, cycle_window_end)]
    SQL filter uses string comparison on ISO timestamps; Python-side
    ``ctx.in_window`` re-checks each row for TZ-naive substrate rows.
    Pagination via LIMIT/OFFSET so cycles with >page_size events still
    surface every row.
    """
    lower_iso = ctx.lower_bound_iso
    upper_iso = ctx.upper_bound_iso

    offset = 0
    while True:
        async with acquire_db() as db:
            cur = await db.execute(
                "SELECT id, source, status, default_program, title, url, "
                "       workspace_id, updated_at "
                "FROM inbox_items "
                "WHERE updated_at > ? AND updated_at <= ? "
                "ORDER BY updated_at ASC "
                "LIMIT ? OFFSET ?",
                (lower_iso, upper_iso, _PAGE_SIZE, offset),
            )
            rows = await cur.fetchall()
        if not rows:
            break

        for row in rows:
            row_id = row[0] if not hasattr(row, "keys") else row["id"]
            source_value = row[1] if not hasattr(row, "keys") else row["source"]
            status = row[2] if not hasattr(row, "keys") else row["status"]
            default_program = (
                row[3] if not hasattr(row, "keys") else row["default_program"]
            )
            title = row[4] if not hasattr(row, "keys") else row["title"]
            url = row[5] if not hasattr(row, "keys") else row["url"]
            updated_at_raw = row[7] if not hasattr(row, "keys") else row["updated_at"]

            observed_at = normalize_iso(updated_at_raw) or ctx.now
            if not ctx.in_window(observed_at):
                continue

            event_type = _classify_event_type(source_value)
            ref = _source_ref(str(row_id), source_value, url)
            project, program = resolve_source_project(default_program)

            evidence = {
                "item_id": str(row_id),
                "source_kind": source_value,
                "status": status,
                "project_hint": default_program,
                "updated_at": observed_at.isoformat(),
            }
            if url:
                evidence["url"] = url

            yield EventDraft(
                event_type=event_type,
                source_system="ingest",
                source_ref=ref,
                title=(title or f"Ingest item {row_id}")[:200],
                summary=f"{source_value or 'inbox'} item status={status}",
                observed_at=observed_at,
                derived_from_state_at=observed_at,
                evidence=evidence,
                source_project=project,
                program_key=program,
            )

        if len(rows) < _PAGE_SIZE:
            break
        offset += _PAGE_SIZE


ingestor_collector = SourceCollector(source_system="ingest", collect=collect)


__all__ = ["collect", "ingestor_collector"]
