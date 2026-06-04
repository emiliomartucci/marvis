# v0.2.0 - 2026-04-16 - KG Phase 6.8: cross-source dedupe via _normalize_key
# v0.1.0 - 2026-04-16 - KG Phase 6.5 A: Reciprocal Rank Fusion helpers
"""Reciprocal Rank Fusion for hybrid retrieval.

Cormack 2009 (classic). `k=60` is the conservative default; probe battery
sweeps `k=10/20/40/60` on representative queries to pick the empirical
best for our corpus. Weights are per-source so the caller can tune
semantic / FTS / KG contributions independently.

Formula (1-indexed ranks):
    score(item) = sum_over_sources( weight[source] / (k + rank + 1) )
where rank is 0-indexed in the input sequence (highest-ranked first).

Phase 6.8: adds ``normalize_key()`` — a helper that maps namespaced fusion
keys ("sem:task:uuid", "task_fts:uuid", "learn_fts:uuid", ...) to a canonical
``(doc_type, doc_id)`` tuple so that cross-source duplicates merge into a
single fused entry with SUMMED contributions (cumulative RRF). Without
this, the same task hit twice (semantic + tasks_fts) consumes two slots in
top-N rather than getting boosted upward.
"""
from __future__ import annotations

from collections.abc import Hashable, Sequence
from typing import Generic, NamedTuple, TypeVar

T = TypeVar("T", bound=Hashable)

DEFAULT_K = 60


class FusionResult(NamedTuple, Generic[T]):
    """Single fused item with its aggregate RRF score and contributing sources."""

    item: T
    score: float
    sources: list[str]


def reciprocal_rank_fusion(
    rankings: dict[str, Sequence[T]],
    weights: dict[str, float] | None = None,
    k: int = DEFAULT_K,
) -> list[FusionResult[T]]:
    """Fuse ranked lists from multiple retrievers using weighted RRF.

    Args:
        rankings: ``{source_name: [item_0, item_1, ...]}``. Items must be
            hashable (used as dict keys). Higher-ranked items first.
        weights: per-source weight. Default: 1.0 each. Unknown weight
            source names are rejected to avoid silent misconfiguration.
        k: fusion constant. Lower k → bigger bonus to top-ranked items
            (more aggressive short-list emphasis). Default 60.

    Returns:
        List of FusionResult tuples sorted by score descending.

    Raises:
        ValueError: if `weights` references a source not in `rankings`.

    Example:
        >>> fused = reciprocal_rank_fusion(
        ...     rankings={"semantic": ["a", "b", "c"], "kg": ["b", "d", "a"]},
        ...     weights={"semantic": 0.5, "kg": 0.5},
        ...     k=60,
        ... )
        >>> [r.item for r in fused][:2]  # top-2 typically includes shared items
        ['b', 'a']
    """
    if weights is None:
        weights = {name: 1.0 for name in rankings}
    unknown = set(weights) - set(rankings)
    if unknown:
        raise ValueError(
            f"weights reference unknown source(s): {unknown}. "
            f"rankings keys: {sorted(rankings)}"
        )

    scores: dict[T, float] = {}
    sources_map: dict[T, list[str]] = {}
    for source_name, ranked_items in rankings.items():
        w = weights.get(source_name, 1.0)
        for rank, item in enumerate(ranked_items):
            contrib = w / (k + rank + 1)
            scores[item] = scores.get(item, 0.0) + contrib
            sources_map.setdefault(item, []).append(source_name)
    return [
        FusionResult(item=item, score=score, sources=sources_map[item])
        for item, score in sorted(scores.items(), key=lambda x: x[1], reverse=True)
    ]


# ---------------------------------------------------------------------------
# Phase 6.8 — cross-source key normalization (dedupe)
# ---------------------------------------------------------------------------


def normalize_key(namespaced_key: str) -> tuple[str, str] | None:
    """Extract a canonical ``(doc_type, doc_id)`` identity from a fusion key.

    Hybrid search namespaces fusion keys per source so retrievers with
    overlapping id-spaces don't collide accidentally. That works for
    kg_fts vs semantic-file (different entities), but HURTS for tasks /
    inbox / learnings where the same row can appear via two lanes:

        semantic    → ``sem:task:<uuid>``
        tasks_fts   → ``task_fts:<uuid>``

    Both refer to the same task row. Without dedupe they consume two slots
    in top-N and their individual RRF contributions never sum. This helper
    returns the canonical identity so the fusion layer (or its caller) can
    bucket+sum contributions.

    Mapping:
        ``sem:<doc_type>:<doc_id>``    → ``(doc_type, doc_id)``
        ``task_fts:<id>``              → ``("task", id)``
        ``inbox_fts:<id>``             → ``("inbox_item", id)``
        ``learn_fts:<id>``             → ``("learning", id)``
        ``kg:<node_id>``               → ``None`` (kg nodes keep their own
                                         identity — a kg:file:artifact is NOT
                                         the same as a semantic file hit with
                                         matching doc_id, they come from
                                         different id-spaces).

    Returns ``None`` when the key has no cross-source twin (kg namespace,
    unknown prefix) — callers should treat such keys as already unique.
    """
    if namespaced_key.startswith("sem:"):
        rest = namespaced_key[4:]  # "<doc_type>:<doc_id>"
        parts = rest.split(":", 1)
        if len(parts) == 2 and parts[0] and parts[1]:
            return (parts[0], parts[1])
        return None
    if namespaced_key.startswith("task_fts:"):
        doc_id = namespaced_key[len("task_fts:") :]
        return ("task", doc_id) if doc_id else None
    if namespaced_key.startswith("inbox_fts:"):
        doc_id = namespaced_key[len("inbox_fts:") :]
        return ("inbox_item", doc_id) if doc_id else None
    if namespaced_key.startswith("learn_fts:"):
        doc_id = namespaced_key[len("learn_fts:") :]
        return ("learning", doc_id) if doc_id else None
    # kg:* and everything else — keep as-is (no canonical twin).
    return None
