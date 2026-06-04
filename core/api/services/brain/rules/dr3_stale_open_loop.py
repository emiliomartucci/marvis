# DR3 — Open Loop Stale (sub-02 §5 DR3, CE4 axis=intent).
# If `journal_entries.body_json.open_loops` from cycle K-N contains a ref AND no
# resolution event in cycles K-N+1..K, emit `stale_open_loop`. Severity ladders
# with cycles_open: low → medium @ 5 → high @ 7 → critical @ 14.
from __future__ import annotations

from datetime import datetime
from typing import Any

from core.api.models.brain import DriftSignal, Severity
from core.api.services.brain.cycle_snapshot import CycleSnapshot, DigestEventRow
from core.api.services.brain.rules._signals import build_signal

_RESOLUTION_MARKERS = frozenset({"merged", "closed", "resolved", "completed"})


def _severity_for_cycles_open(cycles_open: int) -> Severity:
    if cycles_open >= 14:
        return "critical"
    if cycles_open >= 7:
        return "high"
    if cycles_open >= 5:
        return "medium"
    return "low"


def _ref_of(loop: Any) -> str | None:
    if isinstance(loop, dict):
        ref = loop.get("ref") or loop.get("source_ref") or loop.get("event_id")
        if isinstance(ref, str) and ref:
            return ref
    if isinstance(loop, str) and loop:
        return loop
    return None


def _is_resolved_by(event: DigestEventRow, ref: str) -> bool:
    if not isinstance(event.evidence, dict):
        return False
    marker = event.evidence.get("decision_marker")
    if not isinstance(marker, str) or marker not in _RESOLUTION_MARKERS:
        return False
    if event.source_ref == ref:
        return True
    refs = event.evidence.get("resolves")
    if isinstance(refs, list) and ref in refs:
        return True
    if isinstance(refs, str) and refs == ref:
        return True
    return False


async def build_signals(
    snapshot: CycleSnapshot, *, run_id: str, now: datetime
) -> list[DriftSignal]:
    signals: list[DriftSignal] = []
    seen: set[tuple[str, str, str]] = set()
    # Sort prior entries oldest-first so cycles_open computes against the
    # earliest cycle in which the loop was observed.
    prior_sorted = sorted(snapshot.prior_journal_entries, key=lambda e: e.cycle_key)
    for entry in prior_sorted:
        loops = entry.body.get("open_loops") if isinstance(entry.body, dict) else None
        if not isinstance(loops, list):
            continue
        try:
            entry_date = datetime.fromisoformat(entry.cycle_key).date()
            cycle_date = datetime.fromisoformat(snapshot.cycle_key).date()
            cycles_open = max(0, (cycle_date - entry_date).days)
        except ValueError:
            cycles_open = snapshot.lookback_cycles
        if cycles_open <= 0:
            continue
        for loop in loops:
            ref = _ref_of(loop)
            if not ref:
                continue
            key = (entry.scope_type, entry.scope_key, ref)
            if key in seen:
                continue
            seen.add(key)
            # Skip if resolved by any event in current cycle.
            resolved = False
            for ev in snapshot.events:
                if _is_resolved_by(ev, ref):
                    resolved = True
                    break
            if resolved:
                continue
            observed_ref = f"open_loop:{ref}"
            evidence_refs = [
                f"journal_entry:{entry.entry_id}",
                f"open_loop_ref:{ref}",
            ]
            severity_base = _severity_for_cycles_open(cycles_open)
            observed_delta = (
                f"Open loop '{ref}' from cycle {entry.cycle_key} has been "
                f"unresolved for {cycles_open} cycles."
            )
            program_key = entry.program_key
            if entry.scope_type == "project":
                program_key = snapshot.project_program.get(entry.scope_key) or program_key
            signals.append(
                build_signal(
                    run_id=run_id,
                    cycle_key=snapshot.cycle_key,
                    detected_at=now,
                    rule_id="DR3",
                    scope_type=entry.scope_type,  # type: ignore[arg-type]
                    scope_key=entry.scope_key,
                    program_key=program_key,
                    signal_type="stale_open_loop",
                    expected_direction_source="journal",
                    expected_direction_ref=f"journal_entry:{entry.entry_id}",
                    observed_direction_ref=observed_ref,
                    observed_delta=observed_delta,
                    evidence_refs=evidence_refs,
                    severity_base=severity_base,
                    drift_axis="intent",
                    involved_projects=[entry.scope_key]
                    if entry.scope_type == "project"
                    else [],
                )
            )
    return signals


__all__ = ["build_signals"]
