# DR1 — Activity Without Status (sub-02 §5 DR1, CE4 axis=context).
# If a scope has digest activity ≥ N events but no journal/status update for
# the cycle, emit `activity_without_status`.
from __future__ import annotations

from datetime import datetime
from typing import Any

from core.api.models.brain import DriftSignal
from core.api.services.brain.cycle_snapshot import CycleSnapshot, DigestEventRow
from core.api.services.brain.rules._signals import build_signal

ACTIVITY_MIN = 3  # configurable via app_settings post-merge (sub-02 §10)


def _baseline_from_snapshot(
    snapshot: CycleSnapshot, scope_type: str, scope_key: str
) -> tuple[str | None, str]:
    journal = snapshot.journal_entries.get((scope_type, scope_key))
    if journal is not None:
        return (f"journal_entry:{journal.entry_id}", "journal")
    # Look back: most recent prior journal entry for this scope.
    for entry in snapshot.prior_journal_entries:
        if entry.scope_type == scope_type and entry.scope_key == scope_key:
            return (f"journal_entry:{entry.entry_id}", "journal")
    return (None, "none")


def _events_for_scope(
    snapshot: CycleSnapshot, scope_type: str, scope_key: str
) -> list[DigestEventRow]:
    return snapshot.by_scope.get((scope_type, scope_key), [])


def _involved_projects_for_scope(
    scope_type: str, scope_key: str, events: list[DigestEventRow]
) -> list[str]:
    if scope_type == "project":
        return [scope_key]
    projects: set[str] = set()
    for ev in events:
        if ev.source_project:
            projects.add(ev.source_project)
        if ev.target_project:
            projects.add(ev.target_project)
    return sorted(projects)


async def build_signals(
    snapshot: CycleSnapshot, *, run_id: str, now: datetime
) -> list[DriftSignal]:
    signals: list[DriftSignal] = []
    seen: set[tuple[str, str]] = set()
    for (scope_type, scope_key), events in snapshot.by_scope.items():
        if (scope_type, scope_key) in seen:
            continue
        seen.add((scope_type, scope_key))
        if scope_type == "company":
            # Company scope is too coarse — always has activity, status not
            # emitted at company level. Skip per §5 DR1 intent.
            continue
        if len(events) < ACTIVITY_MIN:
            continue
        journal = snapshot.journal_entries.get((scope_type, scope_key))
        if journal is not None and not journal.is_empty:
            continue
        expected_ref, expected_src = _baseline_from_snapshot(
            snapshot, scope_type, scope_key
        )
        observed_ref = f"scope_activity:{scope_type}:{scope_key}:{snapshot.cycle_key}"
        observed_delta = (
            f"Observed {len(events)} digest events for {scope_type}:{scope_key} "
            f"in cycle {snapshot.cycle_key} without a journal/status update."
        )
        evidence_refs = [f"event:{e.event_id}" for e in events[:25]]
        modifiers = 0
        if expected_src == "none":
            modifiers += 1
        signals.append(
            build_signal(
                run_id=run_id,
                cycle_key=snapshot.cycle_key,
                detected_at=now,
                rule_id="DR1",
                scope_type=scope_type,  # type: ignore[arg-type]
                scope_key=scope_key,
                program_key=snapshot.project_program.get(scope_key)
                if scope_type == "project"
                else (scope_key if scope_type == "program" else None),
                signal_type="activity_without_status",
                expected_direction_source=expected_src,  # type: ignore[arg-type]
                expected_direction_ref=expected_ref,
                observed_direction_ref=observed_ref,
                observed_delta=observed_delta,
                evidence_refs=evidence_refs,
                severity_base="medium",
                drift_axis="context",
                involved_projects=_involved_projects_for_scope(
                    scope_type, scope_key, events
                ),
                severity_modifiers=modifiers,
                observed_event=events[0] if events else None,
            )
        )
    return signals


__all__ = ["build_signals", "ACTIVITY_MIN"]
