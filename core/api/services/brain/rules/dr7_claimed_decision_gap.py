# DR7 — Claimed Decision Gap (sub-02 §5 DR7, CE4 axis=intent).
# If an event has decision_marker in {approved, signed_off} AND no matching
# audit_log row with actor_user_id IS NOT NULL referencing the same artifact,
# emit `claimed_decision_gap`. Severity high; critical if claim is external.
#
# Cross-reference: rogue-agent-creates-task-and-PR learning (2026-05-12).
from __future__ import annotations

import logging
from datetime import datetime

from core.api.db import acquire_db
from core.api.models.brain import DriftSignal
from core.api.services.brain.cycle_snapshot import CycleSnapshot, DigestEventRow
from core.api.services.brain.rules._signals import build_signal

logger = logging.getLogger(__name__)

_CLAIM_MARKERS = frozenset({"approved", "signed_off"})


async def _audit_lookup(source_refs: list[str]) -> set[str]:
    """Return the subset of source_refs that have a human-actor audit row."""
    if not source_refs:
        return set()
    placeholders = ",".join("?" for _ in source_refs)
    try:
        async with acquire_db() as db:
            cursor = await db.execute(
                f"SELECT DISTINCT entity_ref FROM audit_log "
                f"WHERE entity_ref IN ({placeholders}) "
                f"AND actor_user_id IS NOT NULL",
                source_refs,
            )
            rows = await cursor.fetchall()
    except Exception as exc:  # noqa: BLE001 — table may not exist
        logger.debug("DR7: audit_log lookup skipped (%s)", exc)
        return set()
    return {row[0] for row in rows}


def _scope(event: DigestEventRow) -> tuple[str, str]:
    if event.source_project:
        return ("project", event.source_project)
    if event.program_key:
        return ("program", event.program_key)
    return ("company", "__company__")


async def build_signals(
    snapshot: CycleSnapshot, *, run_id: str, now: datetime
) -> list[DriftSignal]:
    candidates: list[tuple[DigestEventRow, str]] = []
    for event in snapshot.decision_marker_events:
        marker = event.evidence.get("decision_marker") if isinstance(event.evidence, dict) else None
        if not isinstance(marker, str) or marker not in _CLAIM_MARKERS:
            continue
        candidates.append((event, event.source_ref))
    if not candidates:
        return []

    # Batched lookup (chunked IN, max 500 per chunk — §5 DR7).
    refs = [src_ref for _, src_ref in candidates]
    matched: set[str] = set()
    chunk = 500
    for i in range(0, len(refs), chunk):
        matched |= await _audit_lookup(refs[i : i + chunk])

    signals: list[DriftSignal] = []
    seen: set[str] = set()
    for event, src_ref in candidates:
        if src_ref in matched:
            continue
        if src_ref in seen:
            continue
        seen.add(src_ref)
        scope_type, scope_key = _scope(event)
        observed_ref = f"event:{event.event_id}"
        marker = (
            event.evidence.get("decision_marker")
            if isinstance(event.evidence, dict)
            else "approved"
        )
        observed_delta = (
            f"Decision claim '{marker}' on {src_ref} with no human actor in audit_log."
        )
        external = bool(
            isinstance(event.evidence, dict)
            and event.evidence.get("claimed_by_external")
        )
        severity_base = "critical" if external else "high"
        signals.append(
            build_signal(
                run_id=run_id,
                cycle_key=snapshot.cycle_key,
                detected_at=now,
                rule_id="DR7",
                scope_type=scope_type,  # type: ignore[arg-type]
                scope_key=scope_key,
                program_key=event.program_key,
                signal_type="claimed_decision_gap",
                expected_direction_source="none",
                expected_direction_ref=None,
                observed_direction_ref=observed_ref,
                observed_delta=observed_delta,
                evidence_refs=[observed_ref, f"audit_query:{src_ref}"],
                severity_base=severity_base,
                drift_axis="intent",
                involved_projects=sorted(
                    {
                        p
                        for p in (event.source_project, event.target_project)
                        if isinstance(p, str) and p
                    }
                ),
                observed_event=event,
                decision_marker=marker if isinstance(marker, str) else None,
            )
        )
    return signals


__all__ = ["build_signals"]
