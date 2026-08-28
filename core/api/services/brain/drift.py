# Brain v1 — Drift Checker orchestrator (sub-02 §6 C1) + persistence (C4).
#
# Drift runs as PHASE 3 of the same brain_runs envelope. The orchestrator:
#   1. Builds a CycleSnapshot (read-only L2 projection).
#   2. Invokes each DR rule with a 10s budget; per-rule failures isolated.
#   3. Persists signals via INSERT OR IGNORE.
#   4. Updates supersede chain across superseded brain_runs (same recurrence_key).
#
# Layering invariant: this module does NOT issue raw SQL on substrate tables.
# Reads go through journal API + cycle_snapshot; writes only to brain_drift_signals.
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from core.api.db import write_db
from core.api.models.brain import DriftSignal
from core.api.services.brain.cycle_snapshot import CycleSnapshot, build_snapshot
from core.api.services.brain.rules import DR_AXIS_MATRIX, active_rules
from core.api.services.brain.rules._signals import CONFIDENCE_FLOOR

logger = logging.getLogger(__name__)

DEFAULT_RULE_TIMEOUT_S = 10
DEFAULT_LOOKBACK_CYCLES = 7

# Wave 3.1 gap 1: signals with rule_id='DR8' AND confidence >= this threshold
# are promoted to a `direction_drift` finding via emit_finding_dedup. Below
# the threshold they stay as audit-only drift rows.
DR8_FINDING_CONFIDENCE_THRESHOLD = 0.85

# P4-F1: promotion generalised from the DR8-only hardcode into a matrix.
# rule_id -> (finding_type, confidence_threshold). A drift signal at or above its
# rule's threshold is promoted to an APPROVABLE L5 finding (never an auto-close —
# the P1 producer notifies the owner/grantees, who decide). Adding a promotable
# rule = one entry here + a branch in `_finding_content`.
FINDING_PROMOTION_MATRIX: dict[str, tuple[str, float]] = {
    "DR8": ("direction_drift", DR8_FINDING_CONFIDENCE_THRESHOLD),
    "DR9": ("task_probably_done", 0.6),  # both DR9 levels (0.9 PR-merged, 0.6 handoff) promote
}


def _extract_proposed_payload(signal: DriftSignal) -> dict | None:
    """Pull `proposed_payload_json:{...}` off signal evidence list.

    DR8 appends a string-encoded JSON payload to evidence when the LLM
    classifier (tier-fast) returns a non-aligned status. We round-trip it so
    the finding payload carries the structured direction proposal.
    """
    for ref in signal.evidence or []:
        if isinstance(ref, str) and ref.startswith("proposed_payload_json:"):
            try:
                return json.loads(ref.split(":", 1)[1])
            except (json.JSONDecodeError, ValueError):
                continue
    return None


def _finding_content(
    sig: DriftSignal, finding_type: str
) -> tuple[str, str, str, str, dict, str]:
    """(title, summary, why_now, entity_ref, payload, suggested_artifact) per finding_type."""
    if finding_type == "task_probably_done":
        task_ref = sig.expected_direction_ref or f"task:{sig.scope_key}"
        entity_ref = f"task_probably_done:{task_ref}"  # stable per task -> dedup, no re-run dup
        payload = {
            "drift_signal_id": sig.signal_id,
            "task_ref": task_ref,
            "observed_delta": sig.observed_delta,
            "observed_direction_ref": sig.observed_direction_ref,
        }
        title = (f"Task probabilmente conclusa — {sig.scope_key}")[:200]
        summary = (sig.observed_delta or "task appears resolved")[:2000]
        why_now = (
            f"DR9 al confidence={sig.confidence:.2f}; recurrence_key={sig.recurrence_key}"
        )[:500]
        return (title, summary, why_now, entity_ref, payload, "status_update")

    # direction_drift (DR8) — content contract unchanged (wave-3.1 regression tests).
    proposed = _extract_proposed_payload(sig) or {}
    entity_ref = f"direction_drift:{sig.scope_key}"
    payload = {
        "drift_signal_id": sig.signal_id,
        "observed_delta": sig.observed_delta,
        "expected_direction_ref": sig.expected_direction_ref,
        "observed_direction_ref": sig.observed_direction_ref,
        **proposed,
    }
    title = f"Direction drift — {sig.scope_key}"
    summary = (sig.observed_delta or "direction misalignment observed")[:2000]
    why_now = (
        f"DR8 emitted at confidence={sig.confidence:.2f}; "
        f"recurrence_key={sig.recurrence_key}"
    )
    return (title, summary, why_now, entity_ref, payload, "none")


async def _emit_direction_drift_findings(
    *, run_id: str, cycle_key: str, signals: list[DriftSignal]
) -> int:
    """Promote drift signals to L5 findings via ``FINDING_PROMOTION_MATRIX``.

    A signal at/above its rule's threshold becomes an APPROVABLE finding (never an
    auto-close — the P1 producer notifies the owner/grantees, who decide). Name kept
    for the DR8 wave-3.1 regression tests; the body is now generic over the matrix
    (DR8 direction_drift @0.85 + DR9 task_probably_done @0.6). Returns the count of
    dedup emit calls (new or boost). Failures are logged and swallowed — the cycle
    must finish even if the finding emit path fails.
    """
    from core.api.services.brain.findings import emit_finding_dedup

    emitted = 0
    for sig in signals:
        promo = FINDING_PROMOTION_MATRIX.get(sig.rule_id)
        if promo is None:
            continue
        finding_type, threshold = promo
        if sig.confidence < threshold:
            continue
        title, summary, why_now, entity_ref, payload, suggested = _finding_content(
            sig, finding_type
        )
        try:
            await emit_finding_dedup(
                finding_type=finding_type,  # type: ignore[arg-type]
                entity_ref=entity_ref,
                payload=payload,
                confidence_numeric=sig.confidence,
                scope_type=sig.scope_type,
                scope_key=sig.scope_key,
                cycle_key=cycle_key,
                run_id=run_id,
                title=title,
                summary=summary,
                why_now=why_now,
                severity=sig.severity or "medium",
                suggested_artifact=suggested,  # type: ignore[arg-type]
                program_key=sig.program_key,
                evidence_refs=[f"drift_signal:{sig.signal_id}"],
            )
            emitted += 1
        except Exception:  # noqa: BLE001 — finding emit is best-effort
            logger.exception(
                "drift: emit_finding_dedup failed for signal_id=%s", sig.signal_id
            )
    return emitted


@dataclass(slots=True)
class DriftRunReport:
    """Return envelope for drift.run_phase()."""

    run_id: str
    cycle_key: str
    signal_count: int = 0
    suppressed_below_floor: int = 0
    direction_drift_findings_emitted: int = 0
    partial_failures: list[dict[str, str]] = field(default_factory=list)


def _utc_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


async def _run_one_rule(
    rule_id: str,
    builder,
    *,
    snapshot: CycleSnapshot,
    run_id: str,
    now: datetime,
    timeout_s: int,
) -> list[DriftSignal]:
    """Execute one rule with timeout + per-rule failure isolation."""
    try:
        async with asyncio.timeout(timeout_s):
            result = await builder(snapshot, run_id=run_id, now=now)
            return list(result)
    except asyncio.TimeoutError:
        logger.warning("drift: rule %s exceeded %ds budget", rule_id, timeout_s)
        raise
    except Exception:  # noqa: BLE001 — caller appends to partial_failures
        logger.exception("drift: rule %s raised", rule_id)
        raise


def _validate_axis(signal: DriftSignal) -> DriftSignal:
    """Invariant 11: per-DR axis matches matrix. Defensive — should never fail
    in production because rules hard-code the axis from `_signals.build_signal`
    callers — but a regression test would catch a drift-author bug here."""
    expected = DR_AXIS_MATRIX.get(signal.rule_id)
    if expected and signal.drift_axis != expected:
        raise ValueError(
            f"DR axis matrix violation: {signal.rule_id} emitted "
            f"axis={signal.drift_axis!r}, expected {expected!r}"
        )
    return signal


async def _persist_signals(
    *, run_id: str, signals: list[DriftSignal]
) -> tuple[int, list[str]]:
    """INSERT OR IGNORE per signal. Returns (persisted_count, new_recurrence_keys)."""
    if not signals:
        return (0, [])
    new_recurrence_keys: list[str] = []
    persisted = 0
    async with write_db() as db:
        for sig in signals:
            row = await (
                await db.execute(
                    "SELECT 1 FROM brain_drift_signals WHERE signal_id = ?",
                    (sig.signal_id,),
                )
            ).fetchone()
            if row is not None:
                continue
            await db.execute(
                "INSERT INTO brain_drift_signals ("
                " signal_id, run_id, cycle_key, detected_at, rule_id, schema_version,"
                " scope_type, scope_key, program_key, signal_type, knowledge_form,"
                " classifier_version, expected_direction_source, expected_direction_ref,"
                " observed_direction_ref, observed_delta, evidence_json, evidence_hash,"
                " severity, confidence, recurrence_key, involved_projects_json, state,"
                " superseded_by_signal_id, resolved_at, dismissed_at, dismissed_by,"
                " dismiss_reason, drift_axis"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,"
                " ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    sig.signal_id,
                    sig.run_id,
                    sig.cycle_key,
                    _utc_iso(sig.detected_at),
                    sig.rule_id,
                    sig.schema_version,
                    sig.scope_type,
                    sig.scope_key,
                    sig.program_key,
                    sig.signal_type,
                    sig.knowledge_form,
                    sig.classifier_version,
                    sig.expected_direction_source,
                    sig.expected_direction_ref,
                    sig.observed_direction_ref,
                    sig.observed_delta,
                    json.dumps(sig.evidence, sort_keys=True, ensure_ascii=False),
                    sig.evidence_hash,
                    sig.severity,
                    sig.confidence,
                    sig.recurrence_key,
                    json.dumps(
                        sig.involved_projects, sort_keys=True, ensure_ascii=False
                    ),
                    sig.state,
                    sig.superseded_by_signal_id,
                    _utc_iso(sig.resolved_at) if sig.resolved_at else None,
                    _utc_iso(sig.dismissed_at) if sig.dismissed_at else None,
                    sig.dismissed_by,
                    sig.dismiss_reason,
                    sig.drift_axis,
                ),
            )
            persisted += 1
            new_recurrence_keys.append(sig.recurrence_key)
    return (persisted, new_recurrence_keys)


async def _supersede_prior(
    *, run_id: str, recurrence_keys: list[str]
) -> int:
    """Mark prior open signals with the same recurrence_key as superseded by
    the matching signal in this run."""
    if not recurrence_keys:
        return 0
    superseded = 0
    async with write_db() as db:
        for rkey in set(recurrence_keys):
            new_row = await (
                await db.execute(
                    "SELECT signal_id FROM brain_drift_signals "
                    "WHERE run_id = ? AND recurrence_key = ?",
                    (run_id, rkey),
                )
            ).fetchone()
            if new_row is None:
                continue
            new_signal_id = new_row[0]
            await db.execute(
                "UPDATE brain_drift_signals SET state = 'superseded',"
                " superseded_by_signal_id = ?"
                " WHERE recurrence_key = ?"
                " AND state = 'open'"
                " AND signal_id <> ?",
                (new_signal_id, rkey, new_signal_id),
            )
            superseded += 1
    return superseded


async def run_phase(
    *,
    run_id: str,
    cycle_key: str,
    workspace_id: str = "ws_default",
    now: datetime | None = None,
    rule_timeout_s: int = DEFAULT_RULE_TIMEOUT_S,
    lookback_cycles: int = DEFAULT_LOOKBACK_CYCLES,
) -> DriftRunReport:
    """Execute the drift phase for the given run_id.

    Caller (jobs._execute_cycle) invokes this AFTER `publish_run_journals`
    so the L2 projection is complete.
    """
    started = datetime.now(timezone.utc)
    now = (now or started).astimezone(timezone.utc)
    snapshot = await build_snapshot(
        cycle_key,
        run_id=run_id,
        workspace_id=workspace_id,
        lookback_cycles=lookback_cycles,
        as_of=now,
    )

    all_signals: list[DriftSignal] = []
    partial_failures: list[dict[str, str]] = []
    suppressed = 0

    for rule_id, builder in active_rules():
        try:
            rule_signals = await _run_one_rule(
                rule_id,
                builder,
                snapshot=snapshot,
                run_id=run_id,
                now=now,
                timeout_s=rule_timeout_s,
            )
        except asyncio.TimeoutError:
            partial_failures.append(
                {"kind": "drift_rule_failed", "rule_id": rule_id, "error": "timeout"}
            )
            continue
        except Exception as exc:  # noqa: BLE001
            partial_failures.append(
                {
                    "kind": "drift_rule_failed",
                    "rule_id": rule_id,
                    "error": str(exc)[:500],
                }
            )
            continue
        for sig in rule_signals:
            if sig.confidence < CONFIDENCE_FLOOR:
                suppressed += 1
                logger.info(
                    "drift: suppressed signal_id=%s rule=%s confidence=%.2f",
                    sig.signal_id,
                    sig.rule_id,
                    sig.confidence,
                )
                continue
            _validate_axis(sig)
            all_signals.append(sig)

    persisted, recurrence_keys = await _persist_signals(
        run_id=run_id, signals=all_signals
    )
    await _supersede_prior(run_id=run_id, recurrence_keys=recurrence_keys)

    direction_drift_findings = 0
    try:
        direction_drift_findings = await _emit_direction_drift_findings(
            run_id=run_id, cycle_key=cycle_key, signals=all_signals
        )
    except Exception:  # noqa: BLE001 — finding emit is best-effort
        logger.exception(
            "drift: _emit_direction_drift_findings failed run_id=%s", run_id
        )

    return DriftRunReport(
        run_id=run_id,
        cycle_key=cycle_key,
        signal_count=persisted,
        suppressed_below_floor=suppressed,
        direction_drift_findings_emitted=direction_drift_findings,
        partial_failures=partial_failures,
    )


__all__ = [
    "DEFAULT_LOOKBACK_CYCLES",
    "DEFAULT_RULE_TIMEOUT_S",
    "DriftRunReport",
    "run_phase",
]
