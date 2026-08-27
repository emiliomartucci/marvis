# DR2 — Decision Without ADR (sub-02 §5 DR2, CE4 axis=intent).
# If a commit/PR/handoff has evidence.decision_marker in the whitelist AND no
# ADR/plan artifact is linked via the KG (documents/cites/refers_to), emit
# `decision_without_adr`.
#
# v1 simplification: KG link check is approximated from event.evidence —
# `adr_ref`/`plan_ref`/`linked_artifacts` keys are inspected. v1.1 wires the
# graph_service call with batched chunked IN (max 500).
from __future__ import annotations

import re
from datetime import datetime

from core.api.models.brain import DriftSignal
from core.api.services.brain.cycle_snapshot import CycleSnapshot, DigestEventRow
from core.api.services.brain.rules._signals import build_signal

_DECISION_MARKERS_REQUIRING_ADR = frozenset(
    {"merged", "deployed", "approved", "decision"}
)

# A PR carried in by the pir_tasks collector always has a Marvis ``task_id`` and
# a task-shaped branch (``feat/task-<uuid>`` / ``fix/task-<uuid>``); that IS the
# decision's provenance. Match the first uuid segment (8 hex).
_TASK_BRANCH_RE = re.compile(r"task-[0-9a-f]{8}", re.IGNORECASE)


def _has_task_provenance(ev: dict) -> bool:
    """A merged PR tracked by a Marvis task/plan is NOT an undocumented decision.

    The pir_tasks collector stamps ``task_id`` and ``branch`` on every PR event;
    ignoring them made DR2 fire on every task-tracked PR (audit 2026-08-05,
    task 01752f08: 48 of 50 open findings were this exact false positive).
    """
    task_id = ev.get("task_id")
    if isinstance(task_id, str) and task_id.strip():
        return True
    branch = ev.get("branch")
    if isinstance(branch, str) and _TASK_BRANCH_RE.search(branch):
        return True
    return False


def _has_linked_artifact(event: DigestEventRow) -> bool:
    """Check whether the event already references an ADR/plan/task."""
    ev = event.evidence
    if not isinstance(ev, dict):
        return False
    if ev.get("adr_ref") or ev.get("plan_ref"):
        return True
    if _has_task_provenance(ev):
        return True
    linked = ev.get("linked_artifacts")
    if isinstance(linked, list):
        for item in linked:
            if isinstance(item, dict):
                kind = item.get("kind") or item.get("type")
                if kind in {"adr", "plan", "guide"}:
                    return True
            elif isinstance(item, str) and item.startswith(("adr:", "plan:")):
                return True
    return False


def _scope_for_event(event: DigestEventRow) -> tuple[str, str]:
    """Pick the most-specific scope present on the event."""
    if event.source_project:
        return ("project", event.source_project)
    if event.program_key:
        return ("program", event.program_key)
    return ("company", "__company__")


def _involved_projects(event: DigestEventRow) -> list[str]:
    projects = {
        p
        for p in (event.source_project, event.target_project)
        if isinstance(p, str) and p
    }
    return sorted(projects)


async def build_signals(
    snapshot: CycleSnapshot, *, run_id: str, now: datetime
) -> list[DriftSignal]:
    signals: list[DriftSignal] = []
    seen_refs: set[str] = set()
    for event in snapshot.decision_marker_events:
        marker = event.evidence.get("decision_marker") if isinstance(event.evidence, dict) else None
        if not isinstance(marker, str) or marker not in _DECISION_MARKERS_REQUIRING_ADR:
            continue
        if _has_linked_artifact(event):
            continue
        if event.source_ref in seen_refs:
            continue
        seen_refs.add(event.source_ref)

        scope_type, scope_key = _scope_for_event(event)
        observed_ref = f"event:{event.event_id}"
        observed_delta = (
            f"Decision marker '{marker}' on {event.source_ref} ({event.event_type}) "
            f"without ADR/plan linkage."
        )
        evidence_refs = [observed_ref]
        modifiers = 0
        if event.evidence.get("claimed_by_external") is True:
            modifiers += 1
        signals.append(
            build_signal(
                run_id=run_id,
                cycle_key=snapshot.cycle_key,
                detected_at=now,
                rule_id="DR2",
                scope_type=scope_type,  # type: ignore[arg-type]
                scope_key=scope_key,
                program_key=event.program_key,
                signal_type="decision_without_adr",
                expected_direction_source="doc",
                expected_direction_ref=None,
                observed_direction_ref=observed_ref,
                observed_delta=observed_delta,
                evidence_refs=evidence_refs,
                # Base medium (task 01752f08): a decision with no ADR/plan/task
                # link is worth an open_question, not a high task_candidate.
                # `claimed_by_external` still bumps a genuinely-external one to high.
                severity_base="medium",
                drift_axis="intent",
                involved_projects=_involved_projects(event),
                severity_modifiers=modifiers,
                observed_event=event,
                decision_marker=marker,
            )
        )
    return signals


__all__ = ["build_signals"]
