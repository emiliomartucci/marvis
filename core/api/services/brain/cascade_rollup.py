# Brain v1 — CE3 M8 Cascade Rollup builder (sub-03 §11.5).
#
# Deterministic aggregation: child Journal entries within a scope window →
# proposed append-only patch to parent context.md.
# Disabled by default (`brain_memory_ops_cascade_enabled`).
#
# Invariants (sub-03 §11.5 #8-#12):
#   * Disabled by default. v1.03 baseline behavior unchanged.
#   * Empty window → no operation. (#9)
#   * Cool-down respected. (#10)
#   * Byte-stable bullet ordering: sort by event_type tag then recency DESC. (#11)
#   * Cross-scope leak: project A entries NEVER appear in project B rollup. (#12)
#   * Apply guidance-only: response carries target_path + body suggestion, NEVER writes.
#   * NO LLM polish. Aggregate is mechanical (count + tag + recency).
#   * Append-only patch: NEVER rewrite preamble.
from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import aiosqlite

from core.api.db import acquire_db
from core.api.services.brain.compound_bridge import (
    build_proposed_write_context_md_append,
)
from core.api.services.brain.memory_ops import (
    JournalEntryRow,
    OperationDraft,
    OpSnapshot,
)

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class CascadeSettings:
    enabled: bool = False
    window_days: int = 14
    min_interval_days: int = 7
    max_bullets_per_group: int = 3
    program_scope_enabled: bool = False


_EVENT_TYPE_PRETTY: dict[str, str] = {
    "decision": "Decision",
    "procedure": "Procedure",
    "open_loop": "Open loop",
    "regression_signal": "Regression",
    "external_update": "External update",
    "new_artifact": "Artifact",
}


async def load_cascade_settings(db: aiosqlite.Connection) -> CascadeSettings:
    """Load cascade settings from app_settings. Defaults match sub-03 §11.5."""
    rows = await (
        await db.execute(
            "SELECT key, value FROM app_settings"
            " WHERE key IN ("
            "  'brain_memory_ops_cascade_enabled',"
            "  'brain_memory_ops_cascade_window_days',"
            "  'brain_memory_ops_cascade_min_interval_days',"
            "  'brain_memory_ops_cascade_max_bullets_per_group',"
            "  'brain_memory_ops_cascade_program_scope_enabled'"
            ")"
        )
    ).fetchall()
    cfg = {r[0] if not hasattr(r, "keys") else r["key"]: (r[1] if not hasattr(r, "keys") else r["value"]) for r in rows}

    def _as_int(key: str, default: int) -> int:
        raw = cfg.get(key)
        try:
            return int(raw) if raw is not None else default
        except (TypeError, ValueError):
            return default

    def _as_bool(key: str, default: bool) -> bool:
        raw = cfg.get(key)
        if raw is None:
            return default
        return str(raw).strip().lower() in {"1", "true", "yes", "on"}

    return CascadeSettings(
        enabled=_as_bool("brain_memory_ops_cascade_enabled", False),
        window_days=_as_int("brain_memory_ops_cascade_window_days", 14),
        min_interval_days=_as_int("brain_memory_ops_cascade_min_interval_days", 7),
        max_bullets_per_group=_as_int(
            "brain_memory_ops_cascade_max_bullets_per_group", 3
        ),
        program_scope_enabled=_as_bool(
            "brain_memory_ops_cascade_program_scope_enabled", False
        ),
    )


def _classify_entry(body: dict[str, Any]) -> tuple[str, str]:
    """Map a child journal entry to (tag, title). Deterministic — picks the
    first non-empty section. Order matters (decision > procedure > open_loop
    > regression > external > new_artifact)."""
    if body.get("decisions_observed"):
        items = body["decisions_observed"]
        title = str(items[0]) if items else ""
        return ("decision", title[:120])
    if isinstance(body.get("notable_context"), list) and any(
        isinstance(x, dict) and x.get("kind") == "procedure" for x in body["notable_context"]
    ):
        for x in body["notable_context"]:
            if isinstance(x, dict) and x.get("kind") == "procedure":
                return ("procedure", str(x.get("title", ""))[:120])
    if body.get("open_loops"):
        items = body["open_loops"]
        first = items[0] if items else None
        if isinstance(first, dict):
            return ("open_loop", str(first.get("title", ""))[:120])
        return ("open_loop", str(first or "")[:120])
    if isinstance(body.get("notable_context"), list):
        for x in body["notable_context"]:
            if isinstance(x, dict) and x.get("kind") == "regression":
                return ("regression_signal", str(x.get("title", ""))[:120])
            if isinstance(x, dict) and x.get("kind") == "external_update":
                return ("external_update", str(x.get("title", ""))[:120])
    if body.get("what_changed"):
        items = body["what_changed"]
        first = items[0] if items else None
        if isinstance(first, dict):
            return ("new_artifact", str(first.get("title", ""))[:120])
        return ("new_artifact", str(first or "")[:120])
    return ("new_artifact", "")


@dataclass(slots=True, frozen=True)
class _ScopedEntry:
    entry_id: str
    cycle_key: str
    scope_type: str
    scope_key: str
    tag: str
    title: str
    published_at: datetime


def _group_entries(
    entries: list[JournalEntryRow],
) -> dict[str, list[_ScopedEntry]]:
    """Group classified entries by tag. Cross-scope safety enforced by caller —
    this fn receives entries already filtered to a single parent scope."""
    grouped: dict[str, list[_ScopedEntry]] = defaultdict(list)
    for e in entries:
        tag, title = _classify_entry(e.body)
        grouped[tag].append(
            _ScopedEntry(
                entry_id=e.entry_id,
                cycle_key=e.cycle_key,
                scope_type=e.scope_type,
                scope_key=e.scope_key,
                tag=tag,
                title=title,
                published_at=e.published_at,
            )
        )
    return grouped


def compose_bullets(
    grouped: dict[str, list[_ScopedEntry]],
    *,
    max_bullets_per_group: int,
) -> list[str]:
    """Byte-stable rendering of bullet groups (invariant #11).

    Sort:
      - Groups: alphabetical by tag (deterministic).
      - Within group: by published_at DESC, ties broken by entry_id ASC.
    """
    bullets: list[str] = []
    for tag in sorted(grouped.keys()):
        members = grouped[tag]
        if not members:
            continue
        ordered = sorted(
            members,
            key=lambda x: (-x.published_at.timestamp(), x.entry_id),
        )
        titles = [m.title for m in ordered[:max_bullets_per_group] if m.title]
        if not titles:
            continue
        pretty = _EVENT_TYPE_PRETTY.get(tag, tag.replace("_", " ").title())
        bullets.append(f"- {len(members)} {pretty}: {', '.join(titles)}")
    return bullets


def compose_body(
    *,
    cycle_key: str,
    bullets: list[str],
) -> str:
    """Append-only context.md patch body (never rewrites preamble)."""
    if not bullets:
        return ""
    header = f"## Brain rollup ({cycle_key})"
    return f"{header}\n" + "\n".join(bullets)


def context_md_path_for_project(scope_key: str) -> str:
    """Deterministic context.md path. /data/projects/<slug>/context.md.

    Project-only in v1 — program/company scopes off until pilot validated.
    """
    return f"/data/projects/{scope_key}/context.md"


async def _last_cascade_at_for_scope(
    db: aiosqlite.Connection,
    *,
    scope_type: str,
    scope_key: str,
) -> datetime | None:
    row = await (
        await db.execute(
            "SELECT MAX(detected_at) FROM brain_memory_operations"
            " WHERE operation_type = 'cascade_rollup'"
            "  AND scope_type = ? AND scope_key = ?",
            (scope_type, scope_key),
        )
    ).fetchone()
    if row is None:
        return None
    raw = row[0] if not hasattr(row, "keys") else row[0]
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


async def build_cascade_drafts(
    *,
    snapshot: OpSnapshot,
    now: datetime,
) -> list[OperationDraft]:
    """Build CE3 cascade_rollup proposals for each project scope present in
    the current snapshot. Disabled by default.

    Children: prior journal entries for the project within `window_days`
    (read via secondary DB query — outside the snapshot to capture history).
    Cool-down: skip if prior cascade rollup detected_at < min_interval_days ago.
    """
    async with acquire_db() as db:
        db.row_factory = aiosqlite.Row
        cfg = await load_cascade_settings(db)
        if not cfg.enabled:
            return []

        # Identify project scopes that received a child journal entry this cycle.
        project_scopes: set[str] = set()
        for entry in snapshot.journal_entries:
            if entry.scope_type == "project":
                project_scopes.add(entry.scope_key)

        if not project_scopes:
            return []

        drafts: list[OperationDraft] = []
        cycle_date_str = snapshot.cycle_key
        try:
            ck_date = datetime.fromisoformat(cycle_date_str).date()
        except ValueError:
            return []
        since = (ck_date - timedelta(days=cfg.window_days)).isoformat()

        for scope_key in sorted(project_scopes):
            # Cool-down (invariant #10).
            last_at = await _last_cascade_at_for_scope(
                db, scope_type="project", scope_key=scope_key
            )
            if last_at is not None:
                age = (now - last_at).days
                if age < cfg.min_interval_days:
                    continue

            # Pull child journal entries for THIS project within the window.
            rows = await (
                await db.execute(
                    "SELECT entry_id, cycle_key, scope_type, scope_key, program_key,"
                    " body_json, is_empty, published_at"
                    " FROM brain_journal_entries j"
                    " JOIN brain_runs r ON r.run_id = j.run_id"
                    " WHERE j.workspace_id = ?"
                    "  AND j.scope_type = 'project' AND j.scope_key = ?"
                    "  AND j.cycle_key >= ? AND j.cycle_key <= ?"
                    "  AND j.is_empty = 0"
                    "  AND r.status IN ('succeeded','partial')"
                    "  AND r.superseded_by_run_id IS NULL"
                    " ORDER BY j.cycle_key DESC, j.published_at DESC",
                    (
                        snapshot.workspace_id,
                        scope_key,
                        since,
                        snapshot.cycle_key,
                    ),
                )
            ).fetchall()
            if not rows:
                continue
            entries: list[JournalEntryRow] = []
            import json as _json

            for r in rows:
                pub = r["published_at"] if hasattr(r, "keys") else r[7]
                pub_dt = datetime.fromisoformat(str(pub).replace("Z", "+00:00"))
                if pub_dt.tzinfo is None:
                    pub_dt = pub_dt.replace(tzinfo=timezone.utc)
                body_raw = r["body_json"] if hasattr(r, "keys") else r[5]
                try:
                    body = _json.loads(body_raw or "{}")
                except (TypeError, ValueError):
                    body = {}
                entries.append(
                    JournalEntryRow(
                        entry_id=r["entry_id"] if hasattr(r, "keys") else r[0],
                        cycle_key=r["cycle_key"] if hasattr(r, "keys") else r[1],
                        scope_type=r["scope_type"] if hasattr(r, "keys") else r[2],
                        scope_key=r["scope_key"] if hasattr(r, "keys") else r[3],
                        program_key=r["program_key"] if hasattr(r, "keys") else r[4],
                        body=body if isinstance(body, dict) else {},
                        is_empty=False,
                        published_at=pub_dt,
                    )
                )

            grouped = _group_entries(entries)
            bullets = compose_bullets(
                grouped, max_bullets_per_group=cfg.max_bullets_per_group
            )
            if not bullets:
                continue
            body = compose_body(cycle_key=snapshot.cycle_key, bullets=bullets)
            path = context_md_path_for_project(scope_key)
            child_ids = sorted(set(e.entry_id for e in entries))

            payload = build_proposed_write_context_md_append(
                path=path,
                body=body,
                rollup_cycle_key=snapshot.cycle_key,
                child_entry_ids=child_ids,
            )
            evidence = [f"journal:{eid}" for eid in child_ids]
            drafts.append(
                OperationDraft(
                    operation_type="cascade_rollup",
                    scope_type="project",
                    scope_key=scope_key,
                    program_key=None,
                    source_ref=path,
                    target_ref="",
                    evidence=evidence,
                    summary=(
                        f"Cascade rollup proposal for project {scope_key}: "
                        f"{len(child_ids)} child journal entries in last "
                        f"{cfg.window_days} days."
                    ),
                    proposed_write=payload,
                    involved_projects=[scope_key],
                )
            )
    return drafts


__all__ = [
    "CascadeSettings",
    "build_cascade_drafts",
    "compose_body",
    "compose_bullets",
    "context_md_path_for_project",
    "load_cascade_settings",
]
