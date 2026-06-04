# v1.0.0 - 2026-04-16 - kg node specificity ranker (Phase 7.0 Commit 2)
"""Two-tier KG node ranker: HippoRAG-inspired specificity scoring + same-project boost.

Standalone module — no imports from other kg services, no Pydantic models, no cache.
"""
from __future__ import annotations

import asyncio
from typing import Any

import aiosqlite


RANKER_VERSION = "hipporag-specificity-v1"


async def score_specificity(
    conn: aiosqlite.Connection,
    node_id: str,
    alpha: float = 1.0,
) -> float:
    """Compute HippoRAG specificity score for a node.

    s = 1.0 / (|P| + alpha)

    where |P| = number of distinct projects that contain the same
    ``qualified_name`` (active nodes only). Laplace smoothing via ``alpha``
    prevents division-by-zero on single-project nodes and keeps scores in
    (0, 1].

    Returns 0.0 when the node does not exist or is deprecated.
    """
    cur = await conn.execute(
        "SELECT qualified_name FROM graph_nodes WHERE id = ? AND deprecated_at IS NULL",
        [node_id],
    )
    row = await cur.fetchone()
    if row is None:
        return 0.0

    qualified_name: str = row[0]

    cur = await conn.execute(
        "SELECT COUNT(DISTINCT project_id) FROM graph_nodes "
        "WHERE qualified_name = ? AND deprecated_at IS NULL AND project_id IS NOT NULL",
        [qualified_name],
    )
    count_row = await cur.fetchone()
    project_count: int = count_row[0] if count_row is not None else 0

    return 1.0 / (project_count + alpha)


async def rank_neighbors(
    conn: aiosqlite.Connection,
    neighbors: list[dict[str, Any]],
    project_id: str,
    top_k: int = 10,
) -> list[dict[str, Any]]:
    """Reorder neighbor dicts using two-tier logic.

    Tier 1 boost: same-project neighbors get a ``tier1_boost`` of 1.5;
    cross-project nodes get 1.0.

    Tier 2 specificity: ``score_specificity`` is called for every neighbor
    in parallel (asyncio.gather), then combined with the node's confidence
    base score.

    Final score = confidence_base * tier1_boost * specificity_score

    ``rank_score`` is added to each returned dict (original dict is not
    mutated). Returns at most ``top_k`` results, sorted DESC by rank_score.
    """
    if not neighbors:
        return []

    specificity_scores = await asyncio.gather(
        *[score_specificity(conn, n["id"]) for n in neighbors]
    )

    ranked: list[dict[str, Any]] = []
    for n, spec in zip(neighbors, specificity_scores):
        confidence_base: float = float(n.get("confidence", 0.5)) if n.get("confidence") is not None else 0.5
        tier1_boost: float = 1.5 if n.get("project_id") == project_id else 1.0
        final_score = confidence_base * tier1_boost * spec
        ranked.append({**n, "rank_score": final_score})

    ranked.sort(key=lambda x: x["rank_score"], reverse=True)
    return ranked[:top_k]
