# Learnings source collector (sub-01 §3 — `learning_changed`).
#
# `learnings.updated_at` may be NULL (legacy rows) — fall back to
# `created_at`. `created_at` is `datetime('now','utc')` per migration 028,
# so both fields are UTC-canonicalized via normalize_iso().
from __future__ import annotations

import hashlib
import json
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


def _tags_hash(tags_raw: str | None) -> str:
    try:
        tags = json.loads(tags_raw or "[]")
        if not isinstance(tags, list):
            tags = []
    except (TypeError, json.JSONDecodeError):
        tags = []
    canonical = json.dumps(sorted(str(t) for t in tags), separators=(",", ":"))
    return hashlib.blake2b(canonical.encode("utf-8"), digest_size=8).hexdigest()


# Pagination page size — see ingestor._PAGE_SIZE rationale (bug fix 2026-05-18).
_PAGE_SIZE: int = 500


async def collect(ctx: SourceCollectorContext) -> AsyncIterator[EventDraft]:
    """Yield `learning_changed` events within the cycle window.

    Window = (max(watermark, cycle_window_start), min(cutoff_at, cycle_window_end)]
    Pagination loop (LIMIT/OFFSET) — bug fix 2026-05-18.
    """
    lower_iso = ctx.lower_bound_iso
    upper_iso = ctx.upper_bound_iso

    offset = 0
    while True:
        async with acquire_db() as db:
            cur = await db.execute(
                "SELECT id, title, category, severity, module, project, tags, "
                "       created_at, updated_at "
                "FROM learnings "
                "WHERE COALESCE(updated_at, created_at) > ? "
                "  AND COALESCE(updated_at, created_at) <= ? "
                "ORDER BY COALESCE(updated_at, created_at) ASC "
                "LIMIT ? OFFSET ?",
                (lower_iso, upper_iso, _PAGE_SIZE, offset),
            )
            rows = await cur.fetchall()
        if not rows:
            break

        for row in rows:
            learning_id = row[0] if not hasattr(row, "keys") else row["id"]
            title = row[1] if not hasattr(row, "keys") else row["title"]
            category = row[2] if not hasattr(row, "keys") else row["category"]
            severity = row[3] if not hasattr(row, "keys") else row["severity"]
            module = row[4] if not hasattr(row, "keys") else row["module"]
            project = row[5] if not hasattr(row, "keys") else row["project"]
            tags_raw = row[6] if not hasattr(row, "keys") else row["tags"]
            created_at_raw = row[7] if not hasattr(row, "keys") else row["created_at"]
            updated_at_raw = row[8] if not hasattr(row, "keys") else row["updated_at"]

            observed_at = (
                normalize_iso(updated_at_raw) or normalize_iso(created_at_raw)
            )
            if observed_at is None:
                continue
            if not ctx.in_window(observed_at):
                continue

            source_project, program_key = resolve_source_project(project)
            evidence = {
                "learning_id": str(learning_id),
                "category": category,
                "severity": severity,
                "module": module,
                "tags_hash": _tags_hash(tags_raw),
                "updated_at": observed_at.isoformat(),
            }

            # Decision marker (sub-01 §5.1): a critical learning is treated as a
            # decision when first surfaced. Lower severities propagate as
            # context-only events.
            if severity == "critical":
                evidence["decision_marker"] = "created_with_severity_critical"

            yield EventDraft(
                event_type="learning_changed",
                source_system="learning",
                source_ref=f"learning:{learning_id}",
                title=(title or f"Learning {learning_id}")[:200],
                summary=f"learning category={category} severity={severity}",
                observed_at=observed_at,
                derived_from_state_at=observed_at,
                evidence=evidence,
                source_project=source_project,
                program_key=program_key,
            )

        if len(rows) < _PAGE_SIZE:
            break
        offset += _PAGE_SIZE


learnings_collector = SourceCollector(source_system="learning", collect=collect)


__all__ = ["collect", "learnings_collector"]
