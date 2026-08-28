# Brain v1 — Knowledge form classifier (sub-02 C3).
# Pure deterministic function: signal_type -> KnowledgeForm using the §3.1
# matrix, with first-match-wins ordered rules. Returns ('unknown', 0.5) over
# forcing a weak label.
#
# Reads only typed structured fields (signal_type / event_type / decision_marker
# / evidence.tag). No regex on summary text (anti-pattern).
from __future__ import annotations

from core.api.models.brain import KnowledgeForm, SignalType
from core.api.services.brain.cycle_snapshot import DigestEventRow

CLASSIFIER_VERSION = 1


# Primary form per signal_type (§3.1 matrix).
_PRIMARY: dict[SignalType, KnowledgeForm] = {
    "activity_without_status": "tribal_memory",
    "decision_without_adr": "adr",
    "playbook_changed": "playbook",
    "stale_open_loop": "tribal_memory",
    "docs_governance_drift": "spec",
    "external_update_unpropagated": "external_update",
    "claimed_decision_gap": "claimed_decision",
}

# Allowed secondary forms per signal_type (caller-supplied evidence.tag override).
_ALLOWED_SECONDARY: dict[SignalType, frozenset[KnowledgeForm]] = {
    "activity_without_status": frozenset({"spec"}),
    "decision_without_adr": frozenset({"claimed_decision"}),
    "playbook_changed": frozenset({"spec"}),
    "stale_open_loop": frozenset(),
    "docs_governance_drift": frozenset({"playbook"}),
    "external_update_unpropagated": frozenset(),
    "claimed_decision_gap": frozenset({"adr"}),
}


def _evidence_tag(event: DigestEventRow | None) -> str | None:
    if event is None:
        return None
    tag = event.evidence.get("tag") if isinstance(event.evidence, dict) else None
    if isinstance(tag, str) and tag:
        return tag.strip().lower()
    return None


def classify_knowledge_form(
    signal_type: SignalType,
    *,
    observed_event: DigestEventRow | None = None,
    decision_marker: str | None = None,
) -> tuple[KnowledgeForm, float]:
    """Return (knowledge_form, confidence_modifier).

    Pure function: same inputs → same outputs. The confidence value is a
    *modifier*; the calling DR rule combines it with §4.6 evidence-density.
    """
    primary = _PRIMARY.get(signal_type, "unknown")
    if primary == "unknown":
        return ("unknown", 0.5)

    # Decision_without_adr + claimed_by_external evidence: bump to claimed_decision.
    if signal_type == "decision_without_adr":
        if decision_marker == "claimed":
            return ("claimed_decision", 0.9)
        marker = (
            observed_event.evidence.get("claimed_by_external")
            if observed_event is not None and isinstance(observed_event.evidence, dict)
            else None
        )
        if marker is True:
            return ("claimed_decision", 0.9)

    tag = _evidence_tag(observed_event)
    if tag and tag in _ALLOWED_SECONDARY.get(signal_type, frozenset()):
        return (tag, 1.0)  # type: ignore[return-value]

    return (primary, 1.0)


def classifier_version() -> int:
    return CLASSIFIER_VERSION


__all__ = [
    "CLASSIFIER_VERSION",
    "classifier_version",
    "classify_knowledge_form",
]
