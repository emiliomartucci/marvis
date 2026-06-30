# Brain v1 — Owner hint derivation (sub-04 §7.5).
#
# Deterministic KG-driven owner_hint. NO LLM. Two-tier lookup:
#   1. KG hotspots: scope-matched function/file nodes ordered by touch_count
#      in the window (default 30 days). First non-empty result wins.
#   2. project.default_owner fallback when scope_type='project'.
#
# Graceful degrade — when the graph service is unavailable, missing tables,
# or an exception bubbles up, we return None instead of failing the Learn
# phase. Findings can be built without an owner_hint; the field is non-binding
# and the operator picks freely at apply time.
from __future__ import annotations

import logging
from typing import Any

import aiosqlite

from core.api.db import acquire_db
from core.api.models.brain import OwnerHint

logger = logging.getLogger(__name__)

DEFAULT_HOTSPOT_WINDOW_DAYS = 30
DEFAULT_LIMIT = 3


async def _project_default_owner(
    db: aiosqlite.Connection, project_slug: str
) -> str | None:
    """Look up project.default_owner_user_id. Returns None when the column
    or the row is missing — both are valid in a fresh dev DB."""
    try:
        row = await (
            await db.execute(
                "SELECT value FROM project_metadata"
                " WHERE project = ? AND key = 'default_owner_user_id'",
                (project_slug,),
            )
        ).fetchone()
        if row is not None and row[0]:
            return str(row[0])
    except Exception:
        pass

    try:
        cur = await db.execute(
            "SELECT name FROM pragma_table_info('projects') WHERE name = 'default_owner_user_id'"
        )
        if await cur.fetchone():
            row = await (
                await db.execute(
                    "SELECT default_owner_user_id FROM projects WHERE slug = ?",
                    (project_slug,),
                )
            ).fetchone()
            if row is not None and row[0]:
                return str(row[0])
    except Exception:
        pass
    return None


async def _kg_hotspots(
    db: aiosqlite.Connection,
    scope_type: str,
    scope_key: str,
    *,
    limit: int = DEFAULT_LIMIT,
) -> list[dict[str, Any]]:
    """Best-effort KG hotspot lookup. Returns [] when the graph subsystem
    is unavailable or no nodes match.

    Heuristic: for `project` scope, pull the top-N file nodes by 30-day touch
    count filtered to that project; for `artifact` scope, try qualified_name
    or file_path match. For company/program scopes the hint stays empty —
    those are global enough that an owner suggestion is misleading.
    """
    try:
        cur = await db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='graph_nodes'"
        )
        if await cur.fetchone() is None:
            return []
    except Exception:
        return []

    db.row_factory = aiosqlite.Row
    try:
        if scope_type == "project":
            rows = await (
                await db.execute(
                    "SELECT id, touch_count_30d, touch_authors"
                    " FROM graph_nodes"
                    " WHERE type = 'file'"
                    "   AND project_id = ?"
                    "   AND deprecated_at IS NULL"
                    "   AND touch_count_30d > 0"
                    " ORDER BY touch_count_30d DESC, touch_last_at DESC"
                    " LIMIT ?",
                    (scope_key, limit),
                )
            ).fetchall()
        elif scope_type == "artifact":
            rows = await (
                await db.execute(
                    "SELECT id, touch_count_30d, touch_authors"
                    " FROM graph_nodes"
                    " WHERE type = 'file'"
                    "   AND (qualified_name = ? OR file_path = ?)"
                    "   AND deprecated_at IS NULL"
                    "   AND touch_count_30d > 0"
                    " ORDER BY touch_count_30d DESC, touch_last_at DESC"
                    " LIMIT ?",
                    (scope_key, scope_key, limit),
                )
            ).fetchall()
        else:
            return []
    except Exception as exc:
        logger.debug(
            "owner_hint: KG hotspot lookup failed for %s/%s: %s",
            scope_type, scope_key, exc,
        )
        return []

    import json as _json

    out: list[dict[str, Any]] = []
    for r in rows:
        try:
            authors = _json.loads(r["touch_authors"]) if r["touch_authors"] else []
        except (TypeError, ValueError):
            authors = []
        if not isinstance(authors, list):
            continue
        out.append(
            {
                "node_id": r["id"],
                "touch_count": int(r["touch_count_30d"] or 0),
                "authors": [str(a) for a in authors if a],
            }
        )
    return out


async def compute_owner_hint(
    *,
    scope_type: str,
    scope_key: str,
    db: aiosqlite.Connection | None = None,
) -> OwnerHint | None:
    """Deterministic owner_hint derivation (sub-04 §7.5).

    Returns None when no hint can be derived — never raises. The caller treats
    a missing hint as non-binding (no UI prompt, no auto-assign).
    """
    if not scope_type or not scope_key:
        return None

    own_db = False
    if db is None:
        own_db = True
        cm = acquire_db()
        db = await cm.__aenter__()
    try:
        try:
            hotspots = await _kg_hotspots(db, scope_type, scope_key)
        except Exception:
            hotspots = []

        # Flatten the (file, authors[]) projection into per-author counts.
        author_counts: dict[str, int] = {}
        for row in hotspots:
            for author in row["authors"]:
                author_counts[author] = author_counts.get(author, 0) + row["touch_count"]
        if author_counts:
            ordered = sorted(
                author_counts.items(),
                key=lambda kv: (-kv[1], kv[0]),
            )
            primary, primary_touch = ordered[0]
            alternates = [a for a, _ in ordered[1:DEFAULT_LIMIT]]
            return OwnerHint(
                user_id=primary,
                source="kg_hotspot",
                touch_count=primary_touch,
                alternates=alternates,
                project=scope_key if scope_type == "project" else None,
            )

        if scope_type == "project":
            fallback = await _project_default_owner(db, scope_key)
            if fallback:
                return OwnerHint(
                    user_id=fallback,
                    source="project_default",
                    project=scope_key,
                )

        return None
    finally:
        if own_db:
            await cm.__aexit__(None, None, None)


__all__ = [
    "DEFAULT_HOTSPOT_WINDOW_DAYS",
    "DEFAULT_LIMIT",
    "compute_owner_hint",
]
