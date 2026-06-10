# Brain v1 — Temporal recency pass (Fase D producer, KG freshness/trust).
#
# WHY this exists:
#   The graph now carries graph_nodes.last_verified_at (mig 149) and the
#   read-time needs_review (Fase A + D read-wiring) treats a re-verified node as
#   fresh. This pass is the "suggester": it scans the graph for live nodes that
#   are AGING (last seen a while ago) and NEVER explicitly verified, and PROPOSES
#   a `reinforce` (bump last_verified_at) in the Triage queue. Approving one then
#   calls mark_kg_verified (D write-path) → the node reads as fresh again.
#
# Mechanical, NO LLM (recency is mechanical per the plan). The LLM judgment for
# supersede/contradiction on derived edges is deferred to a follow-up.
#
# Layering invariants (mirror warehouse_consolidate):
#   * NO LLM imports. NO substrate mutation (read-only scan; proposals only).
#   * READ pool (acquire_db) for the scan — never hold the writer across it.
#   * Reuses the memory_ops persist path (finalize_operation + _persist_operations)
#     → proposals land as approval_state='pending', NEVER auto-applied.
#   * Gated by MARVIS_TEMPORAL_MEMORY in jobs.py → dormant when off.
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import aiosqlite

from core.api.db import acquire_db
from core.api.services.brain.compound_bridge import proposed_write_none
from core.api.services.brain.memory_ops import (
    OperationDraft,
    _persist_operations,
    _project_scope,
    finalize_operation,
)

logger = logging.getLogger(__name__)

# Node kinds worth re-verifying (skip noisy/derived kinds like commit/inbox).
RECHECK_TYPES: tuple[str, ...] = (
    "function", "file", "module", "task", "project", "handoff", "learning",
    "doc", "pr", "plan", "solution",
)
# A node is a candidate once it has not been observed for at least this long
# (aging — at risk of false-positive staleness) yet is still live + unverified.
MIN_AGE_DAYS: int = 14
# Hard cap per cycle — proposals are a Triage signal, never a flood.
MAX_CANDIDATES: int = 25
# Re-nag suppression window: a node dismissed/rejected within this many days is
# not re-proposed (respects the operator's "no"). A still-pending proposal is
# always suppressed regardless of age (no duplicate while one is open).
SUPPRESS_DISMISSED_DAYS: int = 30


@dataclass(slots=True, frozen=True)
class _NodeRow:
    node_id: str
    node_type: str
    project_id: str | None
    last_seen_at: str | None


def _parse_iso(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace(" ", "T").replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _scan_query(*, now: datetime) -> tuple[str, list]:
    """(sql, params) for the candidate scan — pure, so it's unit-testable.

    Robust to mixed 'YYYY-MM-DD HH:MM:SS' / ISO-'T' last_seen_at (datetime() both
    sides). The NOT EXISTS guard is the re-nag brake: skip a node that already
    has a recency proposal (reinforce + target_type='none') still PENDING (no
    duplicate while one is open), or DISMISSED/REJECTED within
    SUPPRESS_DISMISSED_DAYS (respect the operator's "no"). Without it the
    cycle-key-scoped operation_id would mint a fresh proposal every night.
    """
    aging_cutoff = (now.astimezone(timezone.utc) - timedelta(days=MIN_AGE_DAYS)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    suppress_cutoff = (
        now.astimezone(timezone.utc) - timedelta(days=SUPPRESS_DISMISSED_DAYS)
    ).strftime("%Y-%m-%d %H:%M:%S")
    placeholders = ",".join("?" * len(RECHECK_TYPES))
    sql = (
        "SELECT n.id, n.type, n.project_id, n.last_seen_at FROM graph_nodes n "
        "WHERE n.deprecated_at IS NULL AND n.last_verified_at IS NULL "
        "AND n.last_seen_at IS NOT NULL "
        f"AND n.type IN ({placeholders}) "
        "AND datetime(n.last_seen_at) <= datetime(?) "
        "AND NOT EXISTS ("
        "  SELECT 1 FROM brain_memory_operations o "
        "  WHERE o.source_ref = n.id "
        "    AND o.operation_type = 'reinforce' "
        "    AND o.proposed_write_target_type = 'none' "
        "    AND (o.approval_state = 'pending' "
        "         OR (o.approval_state IN ('dismissed','rejected') "
        "             AND datetime(o.detected_at) >= datetime(?)))"
        ") "
        "ORDER BY datetime(n.last_seen_at) ASC LIMIT ?"
    )
    params = [*RECHECK_TYPES, aging_cutoff, suppress_cutoff, MAX_CANDIDATES]
    return sql, params


async def _scan_aging_unverified_nodes(*, now: datetime) -> list[_NodeRow]:
    """Live, significant, never-verified, aging nodes — minus those already
    proposed/recently-dismissed (see _scan_query). Read pool; capped, oldest-first.
    """
    sql, params = _scan_query(now=now)
    async with acquire_db() as db:
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute(sql, params)).fetchall()
    return [
        _NodeRow(
            node_id=str(r["id"]),
            node_type=str(r["type"]),
            project_id=r["project_id"],
            last_seen_at=r["last_seen_at"],
        )
        for r in rows
    ]


def build_recency_drafts(nodes: list[_NodeRow], *, now: datetime) -> list[OperationDraft]:
    """One `reinforce` proposal per aging-unverified node. Deterministic.

    proposed_write = none (no CHECK-migration needed); the node id rides in
    source_ref, which the apply-guidance hands to mark_kg_verified. Evidence is
    a single stable ref (`kg_node:<id>`) → stable evidence_hash → idempotent:
    re-detecting the same node bumps recurrence rather than duplicating.
    """
    drafts: list[OperationDraft] = []
    for n in sorted(nodes, key=lambda x: x.node_id):
        scope_type, scope_key = _project_scope(n.project_id)
        seen = _parse_iso(n.last_seen_at)
        age_days = int((now - seen).total_seconds() // 86400) if seen else None
        age_txt = f"{age_days}d ago" if age_days is not None else "long ago"
        # Older → higher priority; clamped to [0, 1].
        score = min(1.0, 0.4 + (age_days or 0) / 180.0)
        drafts.append(
            OperationDraft(
                operation_type="reinforce",
                scope_type=scope_type,
                scope_key=scope_key,
                program_key=None,
                source_ref=n.node_id,
                target_ref="",
                evidence=[f"kg_node:{n.node_id}"],
                summary=(
                    f"Re-verify {n.node_type} {n.node_id}: live but last seen "
                    f"{age_txt}, never verified. Approve → mark_kg_verified bumps "
                    "last_verified_at so it reads fresh again."
                ),
                proposed_write=proposed_write_none(),
                involved_projects=[n.project_id] if n.project_id else [],
                score=score,
            )
        )
    return drafts


async def run_temporal_recency(
    *,
    run_id: str,
    cycle_key: str,
    workspace_id: str = "ws_default",
    now: datetime,
) -> dict[str, int]:
    """Aging-node re-verification pass. PROPOSALS ONLY (approval_state=pending).

    Reuses the memory_ops persist path so re-runs are idempotent (stable
    operation_id collides on the natural key). Returns a small summary dict.
    """
    now = now.astimezone(timezone.utc)
    nodes = await _scan_aging_unverified_nodes(now=now)
    drafts = build_recency_drafts(nodes, now=now)
    operations = [
        finalize_operation(draft=d, run_id=run_id, cycle_key=cycle_key, now=now)
        for d in drafts
    ]
    persisted, _recurrence_keys = await _persist_operations(
        run_id=run_id, operations=operations
    )
    return {"candidates": len(nodes), "operations_persisted": persisted}


__all__ = ["build_recency_drafts", "run_temporal_recency"]
