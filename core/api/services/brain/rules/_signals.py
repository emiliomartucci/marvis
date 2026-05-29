# Shared helpers for DR1..DR7 (sub-02 §4.2).
# Owns deterministic id derivation, evidence hashing, signal factory.
# Private to api/services/brain/rules/ — do NOT import from outside the subpackage.
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Iterable

from core.api.models.brain import (
    DirectionSource,
    DriftAxis,
    DriftSignal,
    KnowledgeForm,
    RuleId,
    ScopeType,
    Severity,
    SignalType,
)
from core.api.services.brain.cycle_snapshot import DigestEventRow
from core.api.services.brain.knowledge_forms import (
    CLASSIFIER_VERSION,
    classify_knowledge_form,
)

CONFIDENCE_FLOOR = 0.3

_SEVERITY_LADDER: tuple[Severity, ...] = ("low", "medium", "high", "critical")


def _utc_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def canonical_evidence(evidence: Iterable[str | dict]) -> str:
    """Stable JSON for hash. Each item is either a TEXT ref or a dict; dicts
    are sort-key serialized. Whole list is sorted by string repr."""
    norm: list[str] = []
    for item in evidence:
        if isinstance(item, str):
            norm.append(item)
        else:
            norm.append(
                json.dumps(item, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
            )
    norm.sort()
    return json.dumps(norm, sort_keys=False, ensure_ascii=False, separators=(",", ":"))


def evidence_hash(evidence: Iterable[str | dict]) -> str:
    """sha256 64-char hex per §4.1 contract."""
    return hashlib.sha256(canonical_evidence(evidence).encode("utf-8")).hexdigest()


def make_signal_id(
    *,
    cycle_key: str,
    rule_id: RuleId,
    scope_type: ScopeType,
    scope_key: str,
    expected_ref: str | None,
    observed_ref: str,
    evidence_hash_hex: str,
) -> str:
    """Stable BLAKE2b-16 hex. EXCLUDES severity, confidence, knowledge_form,
    observed_delta, drift_axis — these are derived metadata (§4.2)."""
    payload = (
        f"{cycle_key}|{rule_id}|{scope_type}|{scope_key}|"
        f"{expected_ref or ''}|{observed_ref}|{evidence_hash_hex}"
    ).encode("utf-8")
    return hashlib.blake2b(payload, digest_size=16).hexdigest()


def make_recurrence_key(
    *,
    rule_id: RuleId,
    scope_type: ScopeType,
    scope_key: str,
    expected_ref: str | None,
    observed_ref: str,
) -> str:
    """BLAKE2b-8 hex without cycle_key — groups same drift across cycles."""
    payload = (
        f"{rule_id}|{scope_type}|{scope_key}|{expected_ref or ''}|{observed_ref}"
    ).encode("utf-8")
    return hashlib.blake2b(payload, digest_size=8).hexdigest()


def _bump_severity(base: Severity, steps: int) -> Severity:
    idx = _SEVERITY_LADDER.index(base) + max(0, steps)
    return _SEVERITY_LADDER[min(idx, len(_SEVERITY_LADDER) - 1)]


def compute_confidence(
    *,
    knowledge_form: KnowledgeForm,
    baseline_source: DirectionSource,
    any_ref_unresolved: bool = False,
) -> float:
    """Deterministic evidence-density formula (§4.6)."""
    confidence = 1.0
    if any_ref_unresolved:
        confidence -= 0.2
    if knowledge_form == "unknown":
        confidence -= 0.2
    if baseline_source == "handoff":
        confidence -= 0.1
    if baseline_source == "none":
        confidence -= 0.3
    return max(CONFIDENCE_FLOOR, min(1.0, round(confidence, 4)))


def build_signal(
    *,
    run_id: str,
    cycle_key: str,
    detected_at: datetime,
    rule_id: RuleId,
    scope_type: ScopeType,
    scope_key: str,
    program_key: str | None,
    signal_type: SignalType,
    expected_direction_source: DirectionSource,
    expected_direction_ref: str | None,
    observed_direction_ref: str,
    observed_delta: str,
    evidence_refs: list[str],
    severity_base: Severity,
    drift_axis: DriftAxis,
    involved_projects: list[str] | None = None,
    severity_modifiers: int = 0,
    observed_event: DigestEventRow | None = None,
    decision_marker: str | None = None,
    any_ref_unresolved: bool = False,
) -> DriftSignal:
    """Construct a DriftSignal with deterministic id / hash / classifier output."""
    evidence_sorted = sorted(set(evidence_refs))
    ev_hash = evidence_hash(evidence_sorted)
    signal_id = make_signal_id(
        cycle_key=cycle_key,
        rule_id=rule_id,
        scope_type=scope_type,
        scope_key=scope_key,
        expected_ref=expected_direction_ref,
        observed_ref=observed_direction_ref,
        evidence_hash_hex=ev_hash,
    )
    recurrence_key = make_recurrence_key(
        rule_id=rule_id,
        scope_type=scope_type,
        scope_key=scope_key,
        expected_ref=expected_direction_ref,
        observed_ref=observed_direction_ref,
    )
    knowledge_form, form_confidence = classify_knowledge_form(
        signal_type, observed_event=observed_event, decision_marker=decision_marker
    )
    confidence = compute_confidence(
        knowledge_form=knowledge_form,
        baseline_source=expected_direction_source,
        any_ref_unresolved=any_ref_unresolved,
    )
    # Form classifier confidence ≤ 1.0 acts as multiplicative floor only when
    # the form is `unknown` (form_confidence=0.5). Don't double-discount.
    if knowledge_form == "unknown":
        confidence = max(CONFIDENCE_FLOOR, min(confidence, form_confidence))
    severity = _bump_severity(severity_base, severity_modifiers)
    return DriftSignal(
        signal_id=signal_id,
        run_id=run_id,
        cycle_key=cycle_key,
        detected_at=detected_at.astimezone(timezone.utc),
        rule_id=rule_id,
        schema_version=1,
        scope_type=scope_type,
        scope_key=scope_key,
        program_key=program_key,
        signal_type=signal_type,
        knowledge_form=knowledge_form,
        classifier_version=CLASSIFIER_VERSION,
        expected_direction_source=expected_direction_source,
        expected_direction_ref=expected_direction_ref,
        observed_direction_ref=observed_direction_ref,
        observed_delta=observed_delta[:2000],
        evidence=evidence_sorted,
        evidence_hash=ev_hash,
        severity=severity,
        confidence=confidence,
        recurrence_key=recurrence_key,
        involved_projects=sorted(set(involved_projects or [])),
        state="open",
        drift_axis=drift_axis,
    )


__all__ = [
    "CONFIDENCE_FLOOR",
    "build_signal",
    "canonical_evidence",
    "compute_confidence",
    "evidence_hash",
    "make_recurrence_key",
    "make_signal_id",
]
