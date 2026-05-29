# Brain v1 — M3 Edge Metrics compatibility shim (sub-03 §6).
#
# Translates `reinforce` operations into a future-compatible `kg_edge_metric`
# proposal. v1 stores the proposal as evidence only — NO mutation of
# `kg_edges` weight. v2 may write to `graph_edge_metrics` directly.
#
# Layering invariant:
#   * No SQL on kg_edges/kg_nodes here.
#   * Manual pin boost reuses sub-03 §4.6 score derivation (deterministic).
from __future__ import annotations

from core.api.models.brain import ProposedWriteKGEdgeMetric


SCORE_BASELINE = 0.5
SCORE_PER_EVIDENCE = 0.1
SCORE_EVIDENCE_CAP = 0.4
SCORE_DRIFT_BOOST = 0.1
SCORE_RECURRENCE_BOOST = 0.1
SCORE_PIN_BOOST = 0.2
SCORE_MULTI_CYCLE_BOOST = 0.1


def compute_reinforce_score(
    *,
    evidence: list[str],
    drift_refs_present: bool = False,
    recurrence_count: int = 1,
    pins_hit: bool = False,
    multi_cycle: bool = False,
) -> float:
    """Deterministic evidence-density score per sub-03 §4.6."""
    score = SCORE_BASELINE
    score += min(SCORE_EVIDENCE_CAP, SCORE_PER_EVIDENCE * len(evidence))
    if drift_refs_present:
        score += SCORE_DRIFT_BOOST
    if recurrence_count >= 3:
        score += SCORE_RECURRENCE_BOOST
    if pins_hit:
        score += SCORE_PIN_BOOST
    if multi_cycle:
        score += SCORE_MULTI_CYCLE_BOOST
    if score < 0.0:
        score = 0.0
    if score > 1.0:
        score = 1.0
    return round(score, 4)


def build_kg_edge_metric_payload(
    *,
    edge_id: str,
    score: float,
    metric_kind: str = "reinforce_score",
) -> ProposedWriteKGEdgeMetric:
    """Construct a ProposedWriteKGEdgeMetric payload.

    `delta` is the increment relative to the existing metric — v1 carries the
    full score as delta from baseline (clamped to [-1.0, 1.0]).
    """
    delta = max(-1.0, min(1.0, score - SCORE_BASELINE))
    return ProposedWriteKGEdgeMetric(
        edge_id=edge_id,
        metric_kind=metric_kind,  # type: ignore[arg-type]
        delta=round(delta, 4),
    )


__all__ = [
    "SCORE_BASELINE",
    "SCORE_DRIFT_BOOST",
    "SCORE_EVIDENCE_CAP",
    "SCORE_MULTI_CYCLE_BOOST",
    "SCORE_PER_EVIDENCE",
    "SCORE_PIN_BOOST",
    "SCORE_RECURRENCE_BOOST",
    "build_kg_edge_metric_payload",
    "compute_reinforce_score",
]
