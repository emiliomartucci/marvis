# DR6 — External Update Unpropagated (sub-02 §5 DR6, CE4 axis=context).
# If `external_update_seen` event has no linked internal artifact within
# `brain_drift_propagation_window_cycles` (default 3), emit
# `external_update_unpropagated`. Severity escalates per missed cycle.
#
# v1.01 note: external_update_seen producer is deferred (sub-01 §3). DR6 will
# be a no-op until that producer ships — tests treat it accordingly.
from __future__ import annotations

from datetime import datetime

from core.api.models.brain import DriftSignal, Severity
from core.api.services.brain.cycle_snapshot import CycleSnapshot, DigestEventRow
from core.api.services.brain.rules._signals import build_signal

# Severity ladder: medium → high @ 2 missed cycles → critical @ 3+ (max).
def _severity_for_missed(missed: int) -> Severity:
    if missed >= 3:
        return "critical"
    if missed >= 2:
        return "high"
    return "medium"


def _propagation_linked(event: DigestEventRow) -> bool:
    if not isinstance(event.evidence, dict):
        return False
    if event.evidence.get("propagation_link_ref"):
        return True
    linked = event.evidence.get("linked_artifacts")
    if isinstance(linked, list) and linked:
        return True
    return False


def _scope(event: DigestEventRow) -> tuple[str, str]:
    if event.source_project:
        return ("project", event.source_project)
    if event.program_key:
        return ("program", event.program_key)
    return ("company", "__company__")


async def build_signals(
    snapshot: CycleSnapshot, *, run_id: str, now: datetime
) -> list[DriftSignal]:
    signals: list[DriftSignal] = []
    seen_refs: set[str] = set()
    for event in snapshot.external_update_events:
        if _propagation_linked(event):
            continue
        if event.source_ref in seen_refs:
            continue
        seen_refs.add(event.source_ref)

        scope_type, scope_key = _scope(event)
        observed_ref = f"event:{event.event_id}"
        # Missed cycles: derived from evidence if available, else default 1.
        missed = 1
        if isinstance(event.evidence, dict):
            raw = event.evidence.get("missed_cycles")
            if isinstance(raw, int) and raw > 0:
                missed = min(raw, 5)
        severity_base = _severity_for_missed(missed)
        observed_delta = (
            f"External update {event.source_ref} observed without internal "
            f"propagation (missed {missed} cycle(s))."
        )
        signals.append(
            build_signal(
                run_id=run_id,
                cycle_key=snapshot.cycle_key,
                detected_at=now,
                rule_id="DR6",
                scope_type=scope_type,  # type: ignore[arg-type]
                scope_key=scope_key,
                program_key=event.program_key,
                signal_type="external_update_unpropagated",
                expected_direction_source="none",
                expected_direction_ref=None,
                observed_direction_ref=observed_ref,
                observed_delta=observed_delta,
                evidence_refs=[observed_ref],
                severity_base=severity_base,
                drift_axis="context",
                involved_projects=sorted(
                    {
                        p
                        for p in (event.source_project, event.target_project)
                        if isinstance(p, str) and p
                    }
                ),
                observed_event=event,
            )
        )
    return signals


__all__ = ["build_signals"]
