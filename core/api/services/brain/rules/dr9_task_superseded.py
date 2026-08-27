# DR9 — Task Superseded (Brain v2 P4-F1, signal_type=task_superseded, axis=intent).
#
# Detects tasks still OPEN (status in approved/in_progress/pending) that look already
# resolved, so the Brain can propose closing them — an APPROVABLE finding, never an
# auto-close. Two deterministic confidence levels (via build_signal confidence_override,
# so the new signal_type is not capped at 0.5 by the evidence-density classifier):
#   (a) an open task with >=1 MERGED pull request  -> 0.9 (strong: the work landed)
#   (b) an open task with a handoff that declares it done (KG handoff node whose
#       title/tags carry a done-marker + frontmatter task_id) -> 0.6 (softer)
# (a) wins over (b) for the same task (no double signal).
#
# Layering: like DR8, this rule reads NON-substrate tables (tasks, pull_requests,
# graph_nodes) via its own acquire_db() — it does not touch the CycleSnapshot digest.
# The tasks table has NO merged_at column; "merged" is pull_requests.status='merged'.

from __future__ import annotations

import logging
from datetime import datetime

import aiosqlite

from core.api.db import acquire_db
from core.api.models.brain import DriftSignal
from core.api.services.brain.cycle_snapshot import CycleSnapshot
from core.api.services.brain.rules._signals import build_signal

logger = logging.getLogger(__name__)

_OPEN_STATUSES = ("approved", "in_progress", "pending")
CONFIDENCE_PR_MERGED = 0.9
CONFIDENCE_HANDOFF_DONE = 0.6
# Soft bound so a first run on a rich tenant cannot flood; log when hit.
MAX_SIGNALS_PER_CYCLE = 200

# Done-markers matched (case-insensitive) against a handoff's title/tags for path (b).
_DONE_MARKERS = ("done", "completat", "conclus", "risolt", "chius", "finished", "complete")


async def _open_tasks_with_merged_pr(
    db: aiosqlite.Connection,
) -> list[tuple[str, str, str, str, int]]:
    """(task_id, project, title, pr_ids_csv, pr_count) for open tasks with >=1 merged PR."""
    placeholders = ",".join("?" for _ in _OPEN_STATUSES)
    cur = await db.execute(
        "SELECT t.id, t.project, t.title, GROUP_CONCAT(pr.id) AS pr_ids, COUNT(pr.id) AS n "
        "FROM tasks t "
        "JOIN pull_requests pr ON pr.task_id = t.id AND pr.status = 'merged' "
        f"WHERE t.status IN ({placeholders}) AND t.deleted_at IS NULL "
        "GROUP BY t.id",
        _OPEN_STATUSES,
    )
    rows = await cur.fetchall()
    await cur.close()
    return [(r[0], r[1], r[2] or "", r[3] or "", int(r[4] or 0)) for r in rows]


async def _open_tasks_declared_done_by_handoff(
    db: aiosqlite.Connection,
) -> list[tuple[str, str, str, str]]:
    """(task_id, project, title, handoff_id) for open tasks whose handoff declares done."""
    like = " OR ".join(
        "lower(json_extract(gn.metadata,'$.title')) LIKE ? "
        "OR lower(json_extract(gn.metadata,'$.tags')) LIKE ?"
        for _ in _DONE_MARKERS
    )
    params: list[str] = []
    for m in _DONE_MARKERS:
        params.extend([f"%{m}%", f"%{m}%"])
    placeholders = ",".join("?" for _ in _OPEN_STATUSES)
    try:
        cur = await db.execute(
            "SELECT DISTINCT t.id, t.project, t.title, gn.id "
            "FROM graph_nodes gn "
            "JOIN tasks t ON t.id = json_extract(gn.metadata,'$.task_id') "
            f"WHERE gn.type = 'handoff' AND t.status IN ({placeholders}) "
            "AND t.deleted_at IS NULL "
            f"AND ({like})",
            (*_OPEN_STATUSES, *params),
        )
        rows = await cur.fetchall()
        await cur.close()
    except Exception:  # noqa: BLE001 — KG may be absent/empty on a fresh tenant
        logger.debug("DR9: handoff-done lookup skipped (KG unavailable)", exc_info=True)
        return []
    return [(r[0], r[1], r[2] or "", r[3]) for r in rows]


def _program_of(snapshot: CycleSnapshot, project: str) -> str | None:
    try:
        meta = snapshot.project_meta.get(project, {})  # type: ignore[attr-defined]
        return meta.get("program")
    except Exception:  # noqa: BLE001
        return None


async def build_signals(
    snapshot: CycleSnapshot, *, run_id: str, now: datetime
) -> list[DriftSignal]:
    """Emit task_superseded drift signals for open tasks that look already resolved."""
    signals: list[DriftSignal] = []

    async with acquire_db() as db:
        merged = await _open_tasks_with_merged_pr(db)
        done_by_handoff = await _open_tasks_declared_done_by_handoff(db)

    seen: set[str] = set()

    def _emit(
        *,
        task_id: str,
        project: str,
        title: str,
        observed_ref: str,
        observed_delta: str,
        evidence_refs: list[str],
        confidence: float,
    ) -> None:
        if not project or task_id in seen:
            return
        if len(signals) >= MAX_SIGNALS_PER_CYCLE:
            return
        seen.add(task_id)
        signals.append(
            build_signal(
                run_id=run_id,
                cycle_key=snapshot.cycle_key,
                detected_at=now,
                rule_id="DR9",
                scope_type="project",
                scope_key=project,
                program_key=_program_of(snapshot, project),
                signal_type="task_superseded",
                expected_direction_source="project_status",
                expected_direction_ref=f"task:{task_id}",
                observed_direction_ref=observed_ref,
                observed_delta=observed_delta[:2000],
                evidence_refs=evidence_refs,
                severity_base="medium",
                drift_axis="intent",
                involved_projects=[project],
                confidence_override=confidence,
            )
        )

    # (a) strongest signal first — an open task whose PR already merged.
    for task_id, project, title, pr_ids_csv, n in merged:
        pr_ids = [p for p in pr_ids_csv.split(",") if p]
        _emit(
            task_id=task_id,
            project=project,
            title=title,
            observed_ref=f"merged_pr:{task_id}",
            observed_delta=(
                f"Task ancora aperta ({title[:120]}) ma {n} PR gia' merged "
                f"[{','.join(pr_ids[:5])}] — probabilmente conclusa."
            ),
            evidence_refs=[f"task:{task_id}", *[f"pr:{p}" for p in pr_ids]],
            confidence=CONFIDENCE_PR_MERGED,
        )

    # (b) softer — a handoff declares the (still-open) task done.
    for task_id, project, title, handoff_id in done_by_handoff:
        _emit(
            task_id=task_id,
            project=project,
            title=title,
            observed_ref=f"handoff_done:{task_id}",
            observed_delta=(
                f"Task ancora aperta ({title[:120]}) ma un handoff la dichiara conclusa "
                f"({handoff_id})."
            ),
            evidence_refs=[f"task:{task_id}", f"handoff:{handoff_id}"],
            confidence=CONFIDENCE_HANDOFF_DONE,
        )

    if len(signals) >= MAX_SIGNALS_PER_CYCLE:
        logger.warning(
            "DR9: hit MAX_SIGNALS_PER_CYCLE=%d — remaining superseded tasks deferred to next cycle",
            MAX_SIGNALS_PER_CYCLE,
        )
    return signals


__all__ = ["build_signals", "CONFIDENCE_PR_MERGED", "CONFIDENCE_HANDOFF_DONE"]
