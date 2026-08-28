# Brain v1 — Direction baseline resolver (sub-02 C2).
# Priority chain: latest journal entry → latest project_status_update → latest
# handoff (auto-handoff). Conflict detection across the three sources.
# Never returns None — uses BaselineReference(source='none', ref=None) sentinel.
#
# Reads ONLY from L2 outputs (journal API) and read-only substrate. Drift rules
# consume the precomputed baselines from CycleSnapshot.baselines.
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from core.api.db import acquire_db
from core.api.models.brain import BaselineReference, DirectionSource, ScopeType
from core.api.services.brain.journal import get_latest_entry

logger = logging.getLogger(__name__)

_EXACT_LEGACY_PROJECT_SQL = (
    "((SELECT COUNT(DISTINCT wp.workspace_id) FROM workspace_projects wp "
    "WHERE wp.project_slug = ?) = 1 AND EXISTS (SELECT 1 FROM workspace_projects wp "
    "WHERE wp.project_slug = ? AND wp.workspace_id = ?)) OR "
    "(NOT EXISTS (SELECT 1 FROM workspace_projects wp WHERE wp.project_slug = ?) "
    "AND (SELECT COUNT(*) FROM workspaces) = 1 "
    "AND EXISTS (SELECT 1 FROM workspaces w WHERE w.id = ?))"
)


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


async def _resolve_project_direction(
    *,
    scope_type: ScopeType,
    scope_key: str,
    as_of: datetime,
    workspace_id: str,
) -> tuple[str | None, datetime | None]:
    """Return (ref, last_updated_at) of the project_directions row for a slug.

    Brain v1.2: this is the priority-1 source of baseline truth for project
    scope when present. Falls through silently when the table is empty
    (cold start before bootstrap rollout) or the project lacks a direction.
    """
    if scope_type != "project":
        return (None, None)
    try:
        async with acquire_db() as db:
            row = await (
                await db.execute(
                    "SELECT project_slug, last_updated_at FROM project_directions"
                    " WHERE project_slug = ? "
                    f"AND ({_EXACT_LEGACY_PROJECT_SQL})",
                    (
                        scope_key,
                        scope_key,
                        scope_key,
                        workspace_id,
                        scope_key,
                        workspace_id,
                    ),
                )
            ).fetchone()
    except Exception as exc:  # noqa: BLE001 — table may not exist on cold DB
        logger.debug("baseline: project_directions lookup skipped (%s)", exc)
        return (None, None)
    if row is None:
        return (None, None)
    slug = row[0] if not hasattr(row, "keys") else row["project_slug"]
    state_at = row[1] if not hasattr(row, "keys") else row["last_updated_at"]
    return (f"project_directions:{slug}", _parse_iso(state_at))


async def _resolve_status_update(
    *, scope_type: ScopeType, scope_key: str, as_of: datetime, workspace_id: str
) -> tuple[str | None, datetime | None]:
    """Return (ref, state_at) of the latest project_status_update for the scope.

    Restricted to project scope (status updates are project-only). For company
    or program scope returns (None, None) — falls through to handoff.
    """
    if scope_type != "project":
        return (None, None)
    cutoff = as_of.astimezone(timezone.utc).isoformat()
    try:
        async with acquire_db() as db:
            row = await (
                await db.execute(
                    "SELECT update_id, created_at FROM project_status_updates "
                    "WHERE project = ? AND created_at <= ? "
                    "AND COALESCE(derived, 0) = 0 "
                    f"AND ({_EXACT_LEGACY_PROJECT_SQL}) "
                    "ORDER BY created_at DESC LIMIT 1",
                    (
                        scope_key,
                        cutoff,
                        scope_key,
                        scope_key,
                        workspace_id,
                        scope_key,
                        workspace_id,
                    ),
                )
            ).fetchone()
    except Exception as exc:  # noqa: BLE001 — table may not exist in test DB
        logger.debug("baseline: status update lookup skipped (%s)", exc)
        return (None, None)
    if row is None:
        return (None, None)
    ref = row[0] if not hasattr(row, "keys") else row["update_id"]
    state_at = row[1] if not hasattr(row, "keys") else row["created_at"]
    return (f"project_status_update:{ref}", _parse_iso(state_at))


async def _resolve_handoff(
    *, scope_type: ScopeType, scope_key: str, as_of: datetime, workspace_id: str
) -> tuple[str | None, datetime | None]:
    """Return (ref, state_at) of the latest auto_handoff for the scope."""
    if scope_type != "project":
        return (None, None)
    cutoff = as_of.astimezone(timezone.utc).isoformat()
    try:
        async with acquire_db() as db:
            row = await (
                await db.execute(
                    "SELECT handoff_id, created_at FROM handoffs "
                    "WHERE project = ? AND created_at <= ? "
                    "AND COALESCE(kind, '') IN ('auto_handoff','handoff') "
                    f"AND ({_EXACT_LEGACY_PROJECT_SQL}) "
                    "ORDER BY created_at DESC LIMIT 1",
                    (
                        scope_key,
                        cutoff,
                        scope_key,
                        scope_key,
                        workspace_id,
                        scope_key,
                        workspace_id,
                    ),
                )
            ).fetchone()
    except Exception as exc:  # noqa: BLE001 — table may not exist
        logger.debug("baseline: handoff lookup skipped (%s)", exc)
        return (None, None)
    if row is None:
        return (None, None)
    ref = row[0] if not hasattr(row, "keys") else row["handoff_id"]
    state_at = row[1] if not hasattr(row, "keys") else row["created_at"]
    return (f"handoff:{ref}", _parse_iso(state_at))


def _direction_marker(payload: Any) -> str | None:
    if isinstance(payload, dict):
        marker = payload.get("direction_marker") or payload.get("direction")
        if isinstance(marker, str):
            return marker.strip().lower() or None
    return None


async def resolve_baseline(
    scope_type: ScopeType,
    scope_key: str,
    *,
    cycle_key: str,
    as_of: datetime,
    workspace_id: str = "ws_default",
    visible_projects: set[str] | None = None,
) -> BaselineReference:
    """Resolve the expected direction baseline for the scope.

    Priority chain (sub-02 C2):
      1. Latest Journal entry within cycle window.
      2. Latest project status update within 7d (derived=False).
      3. Latest handoff within 7d (auto_handoff or handoff kind).

    Returns BaselineReference(source='none') sentinel when nothing matches.
    Conflict detection compares direction markers across resolved sources.
    """
    # Visibility short-circuit: if caller can see only some projects and this
    # one isn't visible, refuse to leak existence.
    if (
        scope_type == "project"
        and visible_projects is not None
        and scope_key not in visible_projects
    ):
        return BaselineReference(source="none", ref=None, confidence=0.7)

    # Brain v1.2 (2026-05-18): project_directions becomes the priority-1
    # source for project scope. Falls back to journal -> status -> handoff
    # when no direction is present (bootstrap not yet applied).
    direction_ref, direction_state_at = await _resolve_project_direction(
        scope_type=scope_type,
        scope_key=scope_key,
        as_of=as_of,
        workspace_id=workspace_id,
    )

    journal = await get_latest_entry(
        scope_type, scope_key, before=cycle_key, workspace_id=workspace_id
    )
    journal_ref: str | None = None
    journal_state_at: datetime | None = None
    journal_marker: str | None = None
    if journal is not None:
        journal_ref = f"journal_entry:{journal.entry_id}"
        journal_state_at = journal.published_at
        journal_marker = _direction_marker(journal.body.model_dump())

    status_ref, status_state_at = await _resolve_status_update(
        scope_type=scope_type, scope_key=scope_key, as_of=as_of, workspace_id=workspace_id
    )
    handoff_ref, handoff_state_at = await _resolve_handoff(
        scope_type=scope_type, scope_key=scope_key, as_of=as_of, workspace_id=workspace_id
    )

    chain: list[tuple[DirectionSource, str | None, datetime | None]] = [
        ("doc", direction_ref, direction_state_at),
        ("journal", journal_ref, journal_state_at),
        ("project_status", status_ref, status_state_at),
        ("handoff", handoff_ref, handoff_state_at),
    ]
    primary: tuple[DirectionSource, str | None, datetime | None] | None = None
    secondary: list[tuple[DirectionSource, str | None, datetime | None]] = []
    for entry in chain:
        if entry[1]:
            if primary is None:
                primary = entry
            else:
                secondary.append(entry)
    if primary is None:
        return BaselineReference(source="none", ref=None, confidence=0.7)

    secondary_refs = [s[1] for s in secondary if s[1]]
    # Conflict: at least two sources present and direction markers disagree.
    # Without direction_marker on status/handoff substrate we only flag conflict
    # if journal_marker exists AND a secondary source disagrees explicitly.
    conflict = False
    if journal_marker and secondary_refs:
        # Best-effort: load secondary direction markers from handoff_summary etc.
        # In v1 we mark conflict if multiple secondaries exist (proxy for
        # divergence pressure). Future v1.1 will compare typed direction tags.
        conflict = len(secondary_refs) >= 2

    source: DirectionSource = primary[0]
    confidence_base = {
        "doc": 1.0,            # project_directions = founder-declared
        "journal": 1.0,
        "project_status": 0.9,
        "handoff": 0.8,
    }.get(source, 0.5)
    if conflict:
        confidence_base = max(0.3, confidence_base - 0.1)

    return BaselineReference(
        source=source,
        ref=primary[1],
        state_at=primary[2],
        confidence=confidence_base,
        conflict=conflict,
        secondary_refs=secondary_refs,
    )


__all__ = ["resolve_baseline"]
