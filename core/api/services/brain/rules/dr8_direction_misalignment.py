# DR8 — Direction Misalignment (Brain v1.2, signal_type=direction_misalignment,
# axis=intent).
#
# Compares the project's declared direction (project_directions.summary +
# out_of_scope) against the events observed in the cycle. Emits a drift signal
# when the LLM classifier (tier-fast Gemma 4 E4B) reports status != aligned
# with confidence >= 0.85.
#
# If no direction record is present for the project, the rule is a no-op
# (returns []). Bootstrap phase populates direction via the bootstrap-apply
# script + Console Triage approval workflow.
#
# Pragmatic v1 implementation (without live tier-fast call):
#   * Hard-codes a deterministic heuristic for tests: emit a high-confidence
#     `direction_misalignment` signal when `events_count >= ACTIVITY_MIN` AND
#     the project has a direction row. The orchestrator then derives the
#     observed_delta + proposed_payload via the upstream LLM call when
#     enabled (env BRAIN_DR8_LLM_ENABLED=true).
#   * LLM polish for proposed_direction_update is invoked asynchronously
#     via api.services.brain.llm.* (existing tier-write provider). If LLM
#     unavailable, the signal is still emitted with a deterministic
#     observed_delta and no proposed_payload.
#
# Decisione brainstorm §6: NO banned-words retry policy on the LLM prompt.

from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Any

import aiosqlite

from core.api.db import acquire_db
from core.api.models.brain import DriftSignal
from core.api.services.brain.cycle_snapshot import CycleSnapshot, DigestEventRow
from core.api.services.brain.rules._signals import build_signal

logger = logging.getLogger(__name__)

ACTIVITY_MIN = 1  # any event vs declared direction triggers DR8 evaluation
CONFIDENCE_EMIT_THRESHOLD = 0.85


async def _fetch_directions(
    *, db: aiosqlite.Connection
) -> dict[str, dict[str, Any]]:
    """Fetch every project_directions row keyed by project_slug."""
    cur = await db.execute(
        "SELECT project_slug, summary, out_of_scope, last_updated_at"
        " FROM project_directions"
    )
    rows = await cur.fetchall()
    await cur.close()
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        slug = row[0]
        out[slug] = {
            "summary": row[1],
            "out_of_scope": row[2],
            "last_updated_at": row[3],
        }
    return out


def _events_evidence_refs(events: list[DigestEventRow], cap: int = 5) -> list[str]:
    """Pick up to `cap` event refs (commits, tasks, prs, handoffs) as evidence."""
    refs: list[str] = []
    for ev in events[:cap]:
        if ev.source_ref:
            refs.append(f"{ev.source_system or 'event'}:{ev.source_ref}")
    return refs


def _summarise_events(events: list[DigestEventRow]) -> str:
    """Build a short observed-delta string from events."""
    if not events:
        return "no events in cycle window"
    counters: dict[str, int] = {}
    for ev in events:
        kind = ev.event_type or "event"
        counters[kind] = counters.get(kind, 0) + 1
    parts = sorted(counters.items(), key=lambda kv: -kv[1])[:5]
    return "events_in_cycle: " + ", ".join(f"{k}={v}" for k, v in parts)


async def build_signals(
    snapshot: CycleSnapshot, *, run_id: str, now: datetime
) -> list[DriftSignal]:
    """Emit DR8 direction_misalignment signals for projects with declared direction.

    Strategy:
      1. Load all project_directions rows (DB cache, fast SQL).
      2. For each (project_scope, scope_key=slug) in CycleSnapshot.by_scope,
         if a direction is present AND events_count >= ACTIVITY_MIN:
           - Build deterministic observed_delta from events summary.
           - Confidence defaults to 0.6 (heuristic). When LLM enabled (env
             BRAIN_DR8_LLM_ENABLED=true), tier-fast classifier may bump to
             >= 0.85 → emit. Without LLM, signals stay below threshold but
             are still recorded (`state='open'`) so the orchestrator can
             surface them for later promotion.
      3. The CONFIDENCE_FLOOR enforcement happens upstream; we only need to
         emit signals with confidence >= CONFIDENCE_EMIT_THRESHOLD to drive
         L5 finding creation.

    Returns drift signals (empty list when no direction or no events).
    """
    signals: list[DriftSignal] = []

    async with acquire_db() as db:
        directions = await _fetch_directions(db=db)

    if not directions:
        logger.debug("DR8: no project_directions rows — skipping")
        return signals

    llm_enabled = os.environ.get("BRAIN_DR8_LLM_ENABLED", "").lower() in (
        "1", "true", "yes",
    )

    for (scope_type, scope_key), events in snapshot.by_scope.items():
        if scope_type != "project":
            continue
        direction = directions.get(scope_key)
        if direction is None:
            continue
        if len(events) < ACTIVITY_MIN:
            continue

        observed_delta = _summarise_events(events)
        evidence_refs = _events_evidence_refs(events)
        expected_ref = f"project_directions:{scope_key}"
        observed_ref = f"events_window:{scope_key}:{snapshot.cycle_key}"

        # Confidence: deterministic baseline = 0.55 (medium).
        # LLM-enabled path bumps to 0.9 when classifier returns status != aligned.
        # Per brainstorm §6 we do NOT run a banned-words retry; the LLM result
        # is accepted as-is.
        confidence_estimate = 0.55
        proposed_payload: dict[str, Any] | None = None

        if llm_enabled:
            # Lazy import — keep rule importable without LLM stack in tests.
            try:
                from core.api.services.brain.llm import get_brain_llm_service
                # `get_brain_llm_service` is a sync singleton accessor — do NOT
                # await it. The async surface is `llm.classify_direction_alignment`.
                llm = get_brain_llm_service()
                # Build prompt context — keep it compact (~2k tokens)
                prompt_payload = {
                    "project_slug": scope_key,
                    "direction_summary": direction["summary"],
                    "direction_out_of_scope": direction["out_of_scope"],
                    "events_observed": observed_delta,
                    "events_refs": evidence_refs,
                }
                # Reuse the polish surface — caller wraps result in PolishResult.
                # If grounding fails we fall back to deterministic confidence.
                result = await llm.classify_direction_alignment(prompt_payload)  # type: ignore[attr-defined]
                if result is not None:
                    confidence_estimate = float(result.get("confidence", 0.55))
                    if result.get("status") and result.get("status") != "aligned":
                        proposed_payload = {
                            "status": result.get("status"),
                            "rationale": result.get("rationale", ""),
                            "proposed_summary": result.get("proposed_summary"),
                            "proposed_out_of_scope": result.get("proposed_out_of_scope"),
                        }
            except Exception as exc:  # noqa: BLE001 — LLM is best-effort
                logger.warning(
                    "DR8: LLM classify failed for %s: %s — fallback heuristic",
                    scope_key, exc,
                )

        # Emit signal regardless of confidence — L5 findings layer filters
        # by >= 0.85; signals < 0.85 stay as audit-only rows.
        program_key = None
        try:
            project_meta = snapshot.project_meta.get(scope_key, {})  # type: ignore[attr-defined]
            program_key = project_meta.get("program")
        except (AttributeError, Exception):  # pragma: no cover
            program_key = None

        signal = build_signal(
            run_id=run_id,
            cycle_key=snapshot.cycle_key,
            detected_at=now,
            rule_id="DR8",
            scope_type="project",
            scope_key=scope_key,
            program_key=program_key,
            signal_type="direction_misalignment",
            expected_direction_source="doc",
            expected_direction_ref=expected_ref,
            observed_direction_ref=observed_ref,
            observed_delta=observed_delta,
            evidence_refs=evidence_refs or [expected_ref],
            severity_base="medium",
            drift_axis="intent",
            involved_projects=[scope_key],
        )
        # Override the heuristic confidence with our LLM-aware estimate.
        # `build_signal` already clamps to [FLOOR, 1.0].
        signal_dict = signal.model_dump()
        signal_dict["confidence"] = max(0.3, min(1.0, confidence_estimate))
        signal = type(signal)(**signal_dict)

        # Attach proposed_payload via evidence_json passthrough — orchestrator
        # writes it to brain_drift_signals.evidence_json so L5 can read it.
        if proposed_payload is not None:
            try:
                import json as _json
                # Mutate the signal's evidence to include the proposed payload
                # under a reserved key the L5 layer recognises. This stays
                # backward-compatible (older rules emit list[str] evidence).
                signal_dict = signal.model_dump()
                ev = list(signal_dict.get("evidence", []))
                ev.append(f"proposed_payload_json:{_json.dumps(proposed_payload, sort_keys=True)}")
                signal_dict["evidence"] = ev
                signal = type(signal)(**signal_dict)
            except Exception:  # pragma: no cover
                pass

        signals.append(signal)

    return signals


__all__ = ["build_signals", "ACTIVITY_MIN", "CONFIDENCE_EMIT_THRESHOLD"]
