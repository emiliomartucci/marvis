# DR5 — Procedure / Playbook Changed (sub-02 §5 DR5, CE4 axis=context).
# If a handoff_changed/learning_changed event's summary matches the compiled
# procedure regex AND no playbook/guide artifact updated in the same cycle,
# emit `playbook_changed`.
#
# CycleSnapshot pre-filters procedure_keyword_hits — scan ONLY summary text
# of digest events. NEVER read handoff files from disk (anti-pattern).
from __future__ import annotations

from datetime import datetime

from core.api.models.brain import DriftSignal
from core.api.services.brain.cycle_snapshot import CycleSnapshot
from core.api.services.brain.rules._signals import build_signal


_ELIGIBLE_EVENT_TYPES = frozenset({"handoff_changed", "learning_changed"})


def _has_playbook_artifact_updated(snapshot: CycleSnapshot) -> bool:
    """Check if any doc_changed event in this cycle is a playbook/guide."""
    for ev in snapshot.by_event_type.get("doc_changed", []):
        ref = ev.source_ref.lower()
        if "playbook" in ref or "guide" in ref:
            return True
        if isinstance(ev.evidence, dict):
            kind = ev.evidence.get("kind")
            if isinstance(kind, str) and kind in {"playbook", "guide"}:
                return True
    return False


async def build_signals(
    snapshot: CycleSnapshot, *, run_id: str, now: datetime
) -> list[DriftSignal]:
    signals: list[DriftSignal] = []
    if not snapshot.procedure_keyword_hits:
        return signals
    playbook_updated = _has_playbook_artifact_updated(snapshot)
    if playbook_updated:
        return signals

    seen_refs: set[str] = set()
    for event in snapshot.procedure_keyword_hits:
        if event.event_type not in _ELIGIBLE_EVENT_TYPES:
            continue
        if event.source_ref in seen_refs:
            continue
        seen_refs.add(event.source_ref)

        if event.source_project:
            scope_type, scope_key = "project", event.source_project
        elif event.program_key:
            scope_type, scope_key = "program", event.program_key
        else:
            scope_type, scope_key = "company", "__company__"

        involved_projects = sorted(
            {
                p
                for p in (event.source_project, event.target_project)
                if isinstance(p, str) and p
            }
        )
        modifiers = 0
        if len(involved_projects) > 1:
            modifiers += 1

        observed_ref = f"event:{event.event_id}"
        observed_delta = (
            f"Procedure-change summary in {event.source_ref} ({event.event_type}) "
            f"without matching playbook/guide update."
        )
        signals.append(
            build_signal(
                run_id=run_id,
                cycle_key=snapshot.cycle_key,
                detected_at=now,
                rule_id="DR5",
                scope_type=scope_type,  # type: ignore[arg-type]
                scope_key=scope_key,
                program_key=event.program_key,
                signal_type="playbook_changed",
                expected_direction_source="doc",
                expected_direction_ref=None,
                observed_direction_ref=observed_ref,
                observed_delta=observed_delta,
                evidence_refs=[observed_ref],
                severity_base="high",
                drift_axis="context",
                involved_projects=involved_projects,
                severity_modifiers=modifiers,
                observed_event=event,
            )
        )
    return signals


__all__ = ["build_signals"]
