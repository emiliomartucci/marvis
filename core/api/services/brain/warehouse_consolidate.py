# Brain v1 — Warehouse consolidation pass (full-store learning dedup).
#
# WHY this exists (the bug it fixes):
#   memory_ops.build_snapshot reads ONLY the current run's digest events
#   (brain_digest_events WHERE run_id = ?). M2 (_m2_consolidate) therefore
#   only dedups "duplicate title within scope on the SAME cycle". Two
#   duplicate learnings created in DIFFERENT cycles are never in the same
#   snapshot → never consolidated.
#
#   This pass scans the WHOLE learnings warehouse (no window filter) and
#   PROPOSES consolidation of same-title same-project groups. It reuses the
#   exact memory_ops persist path (finalize_operation + _persist_operations),
#   so proposals land in the Triage queue as approval_state='pending' and are
#   NEVER auto-applied — a human approves.
#
# Layering invariants (mirror memory_ops):
#   * NO LLM imports (parent §9.3). AST-grep test enforces.
#   * NO mutation of substrate (learnings are read-only here).
#   * Read pool (acquire_db) for the scan; the short persist holds the writer
#     only inside _persist_operations. NEVER hold the write lock across the
#     paginated scan (kg_full_rebuild monopolizing the writer is a known
#     incident — single-writer asyncio.Lock is not reentrant).
#   * Proposals only. No SUPERSEDE/contradiction here (follow-up).
from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone

import aiosqlite

from core.api.db import acquire_db
from core.api.services.brain.compound_bridge import build_proposed_write_doc_patch
from core.api.services.brain.memory_ops import (
    OperationDraft,
    _persist_operations,
    _project_scope,
    finalize_operation,
)

logger = logging.getLogger(__name__)

# Mirror sources/learnings.py pagination page size.
_PAGE_SIZE: int = 500


@dataclass(slots=True, frozen=True)
class _LearningRow:
    learning_id: str
    title: str
    project: str | None
    created_at: str | None


def _parse_iso(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


async def _scan_all_learnings() -> list[_LearningRow]:
    """Read the ENTIRE learnings warehouse via the READ pool, paginated.

    NO window filter — this is the whole point of the warehouse pass. The
    writer is NOT held during the scan; each page opens/closes its own
    read connection.
    """
    out: list[_LearningRow] = []
    offset = 0
    while True:
        async with acquire_db() as db:
            db.row_factory = aiosqlite.Row
            rows = await (
                await db.execute(
                    "SELECT id, title, project, created_at"
                    " FROM learnings"
                    " ORDER BY created_at ASC, id ASC"
                    " LIMIT ? OFFSET ?",
                    (_PAGE_SIZE, offset),
                )
            ).fetchall()
        if not rows:
            break
        for r in rows:
            out.append(
                _LearningRow(
                    learning_id=str(r["id"]),
                    title=(r["title"] or ""),
                    project=r["project"],
                    created_at=r["created_at"],
                )
            )
        if len(rows) < _PAGE_SIZE:
            break
        offset += _PAGE_SIZE
    return out


def _sort_key(member: _LearningRow) -> tuple[datetime, str]:
    """Canonical-choice ordering: OLDEST by created_at, tie-break by id.

    A None created_at sorts last (treated as the far future) so a real
    timestamped row is always preferred as canonical.
    """
    dt = _parse_iso(member.created_at)
    if dt is None:
        dt = datetime.max.replace(tzinfo=timezone.utc)
    return (dt, member.learning_id)


def build_warehouse_drafts(learnings: list[_LearningRow]) -> list[OperationDraft]:
    """Group by (normalized title, project); emit one consolidate draft per
    group with >= 2 members. Deterministic, no DB access.

    Recurrence-key stability: source_ref / target_ref / scope are derived
    purely from the group's normalized title + project + canonical member.
    finalize_operation sorts the evidence set, so the evidence_hash (and thus
    operation_id) is stable across re-runs given the same member set.
    """
    groups: dict[tuple[str, str | None], list[_LearningRow]] = defaultdict(list)
    for m in learnings:
        if not m.title:
            continue
        groups[(m.title.strip().lower(), m.project)].append(m)

    drafts: list[OperationDraft] = []
    for (_norm_title, project), members in groups.items():
        if len(members) < 2:
            continue
        ordered = sorted(members, key=_sort_key)
        canonical = ordered[0]
        duplicates = ordered[1:]
        scope_type, scope_key = _project_scope(project)
        # Evidence = ALL members (canonical + duplicates), sorted-by-id so the
        # evidence_hash is stable irrespective of warehouse scan order.
        evidence_refs = sorted(f"learning:{m.learning_id}" for m in ordered)
        payload = build_proposed_write_doc_patch(
            path=f"learning:{canonical.learning_id}",
            unified_diff=(
                "--- a/duplicate-learnings\n+++ b/canonical-learning\n@@\n"
                "# Consolidate duplicate learnings into the canonical (oldest) one\n"
            ),
            base_sha="",
            rationale=(
                f"{len(ordered)} learnings share title "
                f"'{canonical.title[:80]}' in project "
                f"'{project or '(none)'}'. Canonical (oldest): "
                f"learning:{canonical.learning_id}; duplicates: "
                + ", ".join(f"learning:{d.learning_id}" for d in duplicates)
            ),
        )
        drafts.append(
            OperationDraft(
                operation_type="consolidate",
                scope_type=scope_type,
                scope_key=scope_key,
                program_key=None,
                source_ref=f"learning:{canonical.learning_id}",
                target_ref=f"learning:{duplicates[0].learning_id}",
                evidence=evidence_refs,
                summary=(
                    f"Warehouse consolidate {len(duplicates)} duplicate(s) of "
                    f"'{canonical.title[:80]}' into learning:{canonical.learning_id}."
                ),
                proposed_write=payload,
                involved_projects=[project] if project else [],
            )
        )
    return drafts


async def run_warehouse_consolidation(
    *,
    run_id: str,
    cycle_key: str,
    workspace_id: str = "ws_default",
    now: datetime,
) -> dict[str, int]:
    """Full-store learning dedup pass. PROPOSALS ONLY (approval_state=pending).

    Reuses the memory_ops persist path so re-runs are idempotent: the stable
    operation_id (cycle_key + natural key + evidence_hash) collides on the
    natural unique key and _persist_operations dedups instead of creating a
    new pending proposal. Across cycles, _supersede_prior (run by run_phase
    for window ops) is NOT invoked here — the daily cadence guard in jobs.py
    bounds re-emission; within a cycle, operation_id collision is the guard.

    Returns a small summary dict {groups_found, operations_persisted, skipped}.
    """
    now = now.astimezone(timezone.utc)
    learnings = await _scan_all_learnings()
    drafts = build_warehouse_drafts(learnings)

    # Count groups vs single learnings for the summary.
    groups: dict[tuple[str, str | None], int] = defaultdict(int)
    for m in learnings:
        if m.title:
            groups[(m.title.strip().lower(), m.project)] += 1
    groups_found = sum(1 for n in groups.values() if n >= 2)
    skipped = sum(1 for n in groups.values() if n < 2)

    operations = [
        finalize_operation(draft=d, run_id=run_id, cycle_key=cycle_key, now=now)
        for d in drafts
    ]
    persisted, _recurrence_keys = await _persist_operations(
        run_id=run_id, operations=operations
    )

    return {
        "groups_found": groups_found,
        "operations_persisted": persisted,
        "skipped": skipped,
    }


__all__ = [
    "build_warehouse_drafts",
    "run_warehouse_consolidation",
]
