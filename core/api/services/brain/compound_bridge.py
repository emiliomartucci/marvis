# Brain v1 — Compound integration boundary (sub-03 §6 M2).
#
# This module is the SINGLE seam between L4 Memory Operations and the
# downstream Compound substrate (learning/guide/task/adr/doc_patch).
# Invariants:
#   * No writes — only proposal construction + dry-run validation.
#   * No raw SQL on substrate (tasks/learnings/handoffs).
#   * No LLM imports — deterministic mapping only (parent §9.3).
#   * Operator/agent applies via the apply guidance endpoint; that is the
#     ONLY way an artifact actually gets created.
from __future__ import annotations

from core.api.models.brain import (
    OperationType,
    ProposedWrite,
    ProposedWriteADR,
    ProposedWriteContextMdAppend,
    ProposedWriteDocPatch,
    ProposedWriteGuide,
    ProposedWriteKGEdgeMetric,
    ProposedWriteLearning,
    ProposedWriteNone,
    ProposedWriteTarget,
    ProposedWriteTask,
)

# Static default mapping: operation_type → preferred proposed_write target_type.
# Builders can override (e.g. provenance_hardening → task vs doc_patch depending
# on evidence). This table is the canonical default used by M2/M5/M6.
DEFAULT_TARGET_FOR_OP: dict[OperationType, ProposedWriteTarget] = {
    "reinforce": "kg_edge_metric",
    "consolidate": "doc_patch",
    "supersede_candidate": "doc_patch",
    "provenance_hardening": "task",
    "orphan_detected": "task",
    "contradiction_detected": "task",
    "cascade_rollup": "context_md_append",
    "compression_candidate": "learning",
    # Reserved literals (no producer in v1) — best-effort defaults:
    "deduplicate": "doc_patch",
    "promotion_candidate": "learning",
}


def build_proposed_write_task(
    *,
    operation_id: str,
    title: str,
    description: str,
    project: str,
    impact: int = 5,
    confidence: int = 5,
    ease: int = 5,
    delegation: str = "hybrid",
    priority: str = "medium",
    extra_tags: list[str] | None = None,
) -> ProposedWriteTask:
    """M2 task proposal. Auto-tags with brain_op:{operation_id} for audit chain."""
    tags = list(extra_tags or [])
    audit_tag = f"brain_op:{operation_id}"
    if audit_tag not in tags:
        tags.append(audit_tag)
    return ProposedWriteTask(
        title=title,
        description=description,
        priority=priority,  # type: ignore[arg-type]
        project=project,
        delegation=delegation,  # type: ignore[arg-type]
        impact=impact,
        confidence=confidence,
        ease=ease,
        tags=sorted(set(tags)),
    )


def build_proposed_write_learning(
    *,
    operation_id: str,
    title: str,
    category: str,
    description: str,
    prevention: str,
    severity: str = "medium",
    module: str | None = None,
    project: str | None = None,
    extra_tags: list[str] | None = None,
) -> ProposedWriteLearning:
    tags = list(extra_tags or [])
    audit_tag = f"brain_op:{operation_id}"
    if audit_tag not in tags:
        tags.append(audit_tag)
    return ProposedWriteLearning(
        title=title,
        category=category,  # type: ignore[arg-type]
        description=description,
        prevention=prevention,
        severity=severity,  # type: ignore[arg-type]
        module=module,
        project=project,
        tags=sorted(set(tags)),
    )


def build_proposed_write_doc_patch(
    *,
    path: str,
    unified_diff: str,
    base_sha: str,
    rationale: str,
) -> ProposedWriteDocPatch:
    return ProposedWriteDocPatch(
        path=path,
        unified_diff=unified_diff,
        base_sha=base_sha,
        rationale=rationale,
    )


def build_proposed_write_kg_edge_metric(
    *,
    edge_id: str,
    metric_kind: str = "reinforce_score",
    delta: float = 0.0,
) -> ProposedWriteKGEdgeMetric:
    return ProposedWriteKGEdgeMetric(
        edge_id=edge_id,
        metric_kind=metric_kind,  # type: ignore[arg-type]
        delta=delta,
    )


def build_proposed_write_context_md_append(
    *,
    path: str,
    body: str,
    rollup_cycle_key: str,
    child_entry_ids: list[str],
) -> ProposedWriteContextMdAppend:
    return ProposedWriteContextMdAppend(
        path=path,
        body=body,
        rollup_cycle_key=rollup_cycle_key,
        child_entry_ids=list(child_entry_ids),
    )


def build_proposed_write_guide(
    *,
    path: str,
    title: str,
    body: str,
    source_refs: list[str],
) -> ProposedWriteGuide:
    return ProposedWriteGuide(
        path=path,
        title=title,
        body=body,
        source_refs=list(source_refs),
    )


def build_proposed_write_adr(
    *,
    path: str,
    title: str,
    decision: str,
    context: str,
    consequences: str,
    source_refs: list[str],
    supersedes: list[str] | None = None,
) -> ProposedWriteADR:
    return ProposedWriteADR(
        path=path,
        title=title,
        decision=decision,
        context=context,
        consequences=consequences,
        source_refs=list(source_refs),
        supersedes=list(supersedes or []),
    )


def proposed_write_none() -> ProposedWriteNone:
    return ProposedWriteNone()


def dry_run_proposed_write(
    proposed: ProposedWrite,
) -> tuple[bool, str | None]:
    """Validate a discriminated-union ProposedWrite before INSERT.

    Returns (ok, error_message). The Pydantic model already enforces field
    constraints at construction — this hook is a future extensibility point
    for cross-field checks (e.g. doc_patch base_sha resolvable). v1 returns
    True for any successfully constructed instance.
    """
    if proposed is None:
        return False, "proposed_write is None"
    return True, None


__all__ = [
    "DEFAULT_TARGET_FOR_OP",
    "build_proposed_write_adr",
    "build_proposed_write_context_md_append",
    "build_proposed_write_doc_patch",
    "build_proposed_write_guide",
    "build_proposed_write_kg_edge_metric",
    "build_proposed_write_learning",
    "build_proposed_write_task",
    "dry_run_proposed_write",
    "proposed_write_none",
]
