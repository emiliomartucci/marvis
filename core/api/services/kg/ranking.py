# v1.0.0 - 2026-04-16 - kg node specificity ranker (Phase 7.0 Commit 2)
"""Two-tier KG node ranker: HippoRAG-inspired specificity scoring + same-project boost.

Standalone module — no imports from other kg services, no Pydantic models, no cache.
"""
from __future__ import annotations

import asyncio
from typing import Any

import aiosqlite


RANKER_VERSION = "hipporag-specificity-v1"


def _unambiguous_workspace_project_clause(
    column: str,
    workspace_id: str | None,
    *,
    alias: str,
) -> tuple[str, list[str]]:
    """Quarantine same-slug graph rows until the graph is workspace-keyed."""
    if workspace_id is None:
        return "", []
    owner = f"{alias}_owner"
    other = f"{alias}_other"
    return (
        " AND EXISTS (SELECT 1 FROM workspace_projects "
        f"{owner} WHERE {owner}.workspace_id = ? "
        f"AND {owner}.project_slug = {column})"
        " AND NOT EXISTS (SELECT 1 FROM workspace_projects "
        f"{other} WHERE {other}.project_slug = {column} "
        f"AND {other}.workspace_id <> ?)",
        [workspace_id, workspace_id],
    )


async def score_specificity(
    conn: aiosqlite.Connection,
    node_id: str,
    alpha: float = 1.0,
    *,
    workspace_id: str | None = None,
    visible_projects: set[str] | None = None,
) -> float:
    """Compute HippoRAG specificity score for a node.

    s = 1.0 / (|P| + alpha)

    where |P| = number of distinct projects that contain the same
    ``qualified_name`` (active nodes only). Laplace smoothing via ``alpha``
    prevents division-by-zero on single-project nodes and keeps scores in
    (0, 1].

    Returns 0.0 when the node does not exist or is deprecated.
    """
    params: list[Any] = [node_id]
    workspace_clause, workspace_params = _unambiguous_workspace_project_clause(
        "graph_nodes.project_id", workspace_id, alias="specificity_node"
    )
    params.extend(workspace_params)
    if visible_projects is not None:
        if not visible_projects:
            return 0.0
        ordered_projects = sorted(visible_projects)
        placeholders = ",".join("?" for _ in ordered_projects)
        workspace_clause += f" AND graph_nodes.project_id IN ({placeholders})"
        params.extend(ordered_projects)
    cur = await conn.execute(
        "SELECT qualified_name FROM graph_nodes "
        "WHERE id = ? AND deprecated_at IS NULL" + workspace_clause,
        params,
    )
    row = await cur.fetchone()
    if row is None:
        return 0.0

    qualified_name: str = row[0]

    count_params: list[Any] = [qualified_name]
    count_workspace_clause, count_workspace_params = (
        _unambiguous_workspace_project_clause(
            "graph_nodes.project_id", workspace_id, alias="specificity_count"
        )
    )
    count_params.extend(count_workspace_params)
    if visible_projects is not None:
        ordered_projects = sorted(visible_projects)
        placeholders = ",".join("?" for _ in ordered_projects)
        count_workspace_clause += (
            f" AND graph_nodes.project_id IN ({placeholders})"
        )
        count_params.extend(ordered_projects)
    cur = await conn.execute(
        "SELECT COUNT(DISTINCT project_id) FROM graph_nodes "
        "WHERE qualified_name = ? AND deprecated_at IS NULL "
        "AND project_id IS NOT NULL" + count_workspace_clause,
        count_params,
    )
    count_row = await cur.fetchone()
    project_count: int = count_row[0] if count_row is not None else 0

    return 1.0 / (project_count + alpha)


async def rank_neighbors(
    conn: aiosqlite.Connection,
    neighbors: list[dict[str, Any]],
    project_id: str,
    top_k: int = 10,
    *,
    workspace_id: str | None = None,
    visible_projects: set[str] | None = None,
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
        *[
            score_specificity(
                conn,
                n["id"],
                workspace_id=workspace_id,
                visible_projects=visible_projects,
            )
            for n in neighbors
        ]
    )

    ranked: list[dict[str, Any]] = []
    for n, spec in zip(neighbors, specificity_scores):
        if workspace_id is not None and spec == 0.0:
            # Missing, invisible, or ambiguous nodes must not survive ranking.
            continue
        confidence_base: float = float(n.get("confidence", 0.5)) if n.get("confidence") is not None else 0.5
        tier1_boost: float = 1.5 if n.get("project_id") == project_id else 1.0
        final_score = confidence_base * tier1_boost * spec
        ranked.append({**n, "rank_score": final_score})

    ranked.sort(key=lambda x: x["rank_score"], reverse=True)
    return ranked[:top_k]
