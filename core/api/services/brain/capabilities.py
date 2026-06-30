# Brain v1 — Capabilities (sub-05 §3.1, OD-11).
# Schema metadata for agent cold-start. Mirrors graph_capabilities precedent.
# Returns the Literal members enforced at the Pydantic boundary so agents can
# discover enums without hardcoding constants.
from __future__ import annotations

from typing import get_args

from core.api.models.brain import (
    BrainCapabilities,
    ConfidenceTier,
    DriftAxis,
    EventType,
    FindingApprovalState,
    FindingType,
    KnowledgeForm,
    RunStatus,
    RunTrigger,
    ScopeType,
    Severity,
    SignalState,
    SignalType,
    SourceSystem,
    SuggestedArtifact,
)

# Note: ApprovalState, OperationType, ClosureConditionKind are imported lazily
# to keep this module a leaf (no cycle with models.brain).

KNOWLEDGE_GLYPHS: dict[str, str] = {
    "adr": "▣",
    "spec": "▦",
    "playbook": "⌘",
    "tribal_memory": "✶",
    "external_update": "↘",
    "claimed_decision": "!",
    "unknown": "?",
}


def get_capabilities() -> BrainCapabilities:
    """Snapshot of every Literal enum exposed by the Brain v1 surface."""
    from core.api.models.brain import (
        ApprovalState,
        ClosureConditionKind,
        OperationType,
    )

    def _literal_members(tp: object) -> list[str]:
        return [str(m) for m in get_args(tp)]

    return BrainCapabilities(
        schema_version=1,
        event_types=_literal_members(EventType),
        source_systems=_literal_members(SourceSystem),
        signal_types=_literal_members(SignalType),
        knowledge_forms=_literal_members(KnowledgeForm),
        operation_types=_literal_members(OperationType),
        finding_types=_literal_members(FindingType),
        severities=_literal_members(Severity),
        confidence_tiers=_literal_members(ConfidenceTier),
        drift_axes=_literal_members(DriftAxis),
        approval_states=_literal_members(ApprovalState),
        finding_approval_states=_literal_members(FindingApprovalState),
        signal_states=_literal_members(SignalState),
        run_statuses=_literal_members(RunStatus),
        run_triggers=_literal_members(RunTrigger),
        scope_types=_literal_members(ScopeType),
        suggested_artifacts=_literal_members(SuggestedArtifact),
        closure_condition_kinds=_literal_members(ClosureConditionKind),
        knowledge_glyphs=dict(KNOWLEDGE_GLYPHS),
    )


__all__ = ["KNOWLEDGE_GLYPHS", "get_capabilities"]
