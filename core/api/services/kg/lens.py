# v2.1.0 - 2026-04-17 - prefix node_id in specialized builders (Phase 7.0 fix)
"""KG Inline Lens — builds session-brief KG context for projects and artefact nodes.

Delegates to the primitive layer in ``api.services.kg.queries`` for graph-backed
subqueries and ``api.services.kg.ranking`` for neighbor scoring.

Parallelism via asyncio.gather + per-subquery ``_safe`` wrapper that degrades to
empty on timeout (0.5 s) or error — one slow subquery cannot cancel its peers
(partial-success semantics, deepen-plan R1).
"""
from __future__ import annotations

import asyncio
from typing import Any

import aiosqlite

from core.api.services.kg.queries import (
    _unambiguous_workspace_project_clause,
    fetch_active_neighbors,
    fetch_active_nodes_by_project,
    fetch_cross_project_mentions,
    fetch_hotspot_nodes,
    fetch_node_by_id,
)
from core.api.services.kg.ranking import RANKER_VERSION, rank_neighbors
from core.api.use_cases._context import CallerContext, require_workspace_ctx
from core.api.use_cases._errors import AuthorizationError


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def require_kg_visibility(
    ctx: CallerContext,
    visible_projects: set[str] | None,
) -> tuple[str, set[str] | None]:
    """Bind a deep KG read to authenticated workspace and project grants.

    ``None`` remains the explicit local single-user compatibility contract. A
    hosted/server caller must carry the set resolved by its adapter, including an
    empty set when it has no grants.
    """
    workspace_id = require_workspace_ctx(ctx)
    local_unrestricted = ctx.is_local_os_account and workspace_id == "ws_default"
    if visible_projects is None and not local_unrestricted:
        raise AuthorizationError(
            code="project_visibility_required",
            message="Authenticated project visibility is required",
        )
    return workspace_id, visible_projects


async def _query_learnings(
    conn: aiosqlite.Connection,
    project_id: str | None,
    limit: int,
    workspace_id: str,
    visible_projects: set[str] | None,
) -> list[dict]:
    """Query applicable learnings for a project, degrading gracefully on error.

    Learnings live in a separate schema not guaranteed by KG migrations, so
    errors here must never bubble up to callers.
    """
    if not project_id:
        return []
    if visible_projects is not None and project_id not in visible_projects:
        return []
    try:
        ownership_clause = ""
        ownership_params: list[str] = []
        if visible_projects is not None:
            ownership_clause = (
                " AND EXISTS (SELECT 1 FROM workspace_projects wp "
                "WHERE wp.workspace_id = ? AND wp.project_slug = ?)"
            )
            ownership_params.extend([workspace_id, project_id])
        cur = await conn.execute(
            "SELECT id, title, category, severity, module "
            "FROM learnings "
            "WHERE COALESCE(workspace_id, 'ws_default') = ? "
            "AND (project = ? OR project IS NULL) "
            f"{ownership_clause} "
            "ORDER BY last_occurrence DESC NULLS LAST, created_at DESC "
            "LIMIT ?",
            [workspace_id, project_id, *ownership_params, limit],
        )
        return [dict(r) async for r in cur]
    except Exception:
        return []


async def _fetch_context_chain(
    conn: aiosqlite.Connection,
    node_id: str,
    limit: int,
    workspace_id: str | None,
    visible_projects: set[str] | None,
) -> list[dict]:
    """Return artefact context chain for a node via doc-link relation types."""
    visibility_clause = ""
    visibility_params: list[str] = []
    if visible_projects is not None:
        if not visible_projects:
            return []
        ordered_projects = sorted(visible_projects)
        placeholders = ",".join("?" for _ in ordered_projects)
        visibility_clause = (
            "  AND EXISTS (SELECT 1 FROM graph_nodes visible_anchor "
            "      WHERE visible_anchor.id = e.source_id "
            f"      AND visible_anchor.project_id IN ({placeholders})) "
            f"  AND n2.project_id IN ({placeholders}) "
        )
        visibility_params.extend(ordered_projects)
        visibility_params.extend(ordered_projects)
    anchor_workspace_clause, anchor_workspace_params = (
        _unambiguous_workspace_project_clause(
            "anchor.project_id", workspace_id, alias="chain_anchor"
        )
    )
    target_workspace_clause, target_workspace_params = (
        _unambiguous_workspace_project_clause(
            "n2.project_id", workspace_id, alias="chain_target"
        )
    )
    sql = (
        "SELECT DISTINCT n2.id, n2.type, n2.name, n2.project_id, e.relation "
        "FROM graph_edges e "
        "JOIN graph_nodes n2 ON n2.id = e.target_id "
        "JOIN graph_nodes anchor ON anchor.id = e.source_id "
        "WHERE e.source_id = ? "
        "  AND e.valid_until IS NULL "
        "  AND e.relation IN ('describes', 'produces', 'cites', 'applies_to', 'refers_to', 'mentions') "
        "  AND n2.deprecated_at IS NULL "
        f"{anchor_workspace_clause} "
        f"{target_workspace_clause} "
        f"{visibility_clause}"
        "ORDER BY n2.last_seen_at DESC NULLS LAST "
        "LIMIT ?"
    )
    cur = await conn.execute(
        sql,
        [
            node_id,
            *anchor_workspace_params,
            *target_workspace_params,
            *visibility_params,
            limit,
        ],
    )
    return [dict(r) async for r in cur]


def _make_meta(
    neighbors: list[dict],
    chain: list[dict],
    learnings: list[dict],
    errors: list[dict],
    *,
    deep_effective: bool = False,
    deep_default_source: str = "client",
) -> dict[str, Any]:
    item_count = len(neighbors) + len(chain) + len(learnings)
    return {
        "ranker_version": RANKER_VERSION,
        "item_count": item_count,
        "truncated": False,
        "errors": errors,
        "deep_effective": deep_effective,
        "deep_default_source": deep_default_source,
    }


# ---------------------------------------------------------------------------
# Project-level builder (original, refactored to gather + _safe)
# ---------------------------------------------------------------------------


async def build_kg_context_for_project(
    conn: aiosqlite.Connection,
    slug: str,
    deep: bool = False,
    *,
    ctx: CallerContext,
    visible_projects: set[str] | None,
) -> dict[str, list[dict]]:
    """Four parallel KG subqueries for session_brief.

    Standard: 5 items per bucket. Deep: 15/15/10/15. If a subquery fails
    (missing table, etc.) it degrades to an empty list rather than
    failing the whole brief.
    """
    workspace_id, visible_projects = require_kg_visibility(ctx, visible_projects)
    graph_workspace_id = workspace_id if visible_projects is not None else None
    if visible_projects is not None and slug not in visible_projects:
        return {
            "hotspots": [],
            "recent_nodes": [],
            "cross_mentions": [],
            "applicable_learnings": [],
        }

    limits = (
        {"hotspots": 15, "recent_nodes": 15, "cross_mentions": 10, "learnings": 15}
        if deep
        else {"hotspots": 5, "recent_nodes": 5, "cross_mentions": 5, "learnings": 5}
    )
    errors: list[dict] = []

    async def _safe(name: str, coro):  # type: ignore[no-untyped-def]
        try:
            return await asyncio.wait_for(coro, timeout=0.5)
        except asyncio.TimeoutError:
            errors.append({"subquery": name, "kind": "timeout"})
            return []
        except Exception as e:
            errors.append({"subquery": name, "kind": "error", "message": str(e)})
            return []

    hotspots, recent_nodes, cross_mentions, learnings = await asyncio.gather(
        _safe(
            "hotspots",
            fetch_hotspot_nodes(
                conn,
                slug,
                window_days=30,
                limit=limits["hotspots"],
                workspace_id=graph_workspace_id,
                visible_projects=visible_projects,
            ),
        ),
        _safe(
            "recent_nodes",
            fetch_active_nodes_by_project(
                conn,
                slug,
                order_by="last_seen_at DESC NULLS LAST",
                limit=limits["recent_nodes"],
                workspace_id=graph_workspace_id,
                visible_projects=visible_projects,
            ),
        ),
        _safe(
            "cross_mentions",
            fetch_cross_project_mentions(
                conn,
                slug,
                limit=limits["cross_mentions"],
                workspace_id=graph_workspace_id,
                visible_projects=visible_projects,
            ),
        ),
        _safe(
            "learnings",
            _query_learnings(
                conn,
                slug,
                limits["learnings"],
                workspace_id,
                visible_projects,
            ),
        ),
    )
    return {
        "hotspots": hotspots,
        "recent_nodes": recent_nodes,
        "cross_mentions": cross_mentions,
        "applicable_learnings": learnings,
    }


# ---------------------------------------------------------------------------
# Artefact builders — task / handoff / learning / pr
# ---------------------------------------------------------------------------


async def _build_kg_context_for_node(
    conn: aiosqlite.Connection,
    node_id: str,
    deep: bool,
    *,
    workspace_id: str,
    visible_projects: set[str] | None,
) -> dict[str, Any]:
    """Shared implementation for single-node artefact builders.

    Strategy:
    1. Gather node + neighbors + chain in parallel (fast).
    2. Query learnings sequentially using project_id from node (single fast query).
    """
    limit = 15 if deep else 5
    graph_workspace_id = workspace_id if visible_projects is not None else None
    errors: list[dict] = []

    async def _safe(name: str, coro):  # type: ignore[no-untyped-def]
        try:
            return await asyncio.wait_for(coro, timeout=0.5)
        except asyncio.TimeoutError:
            errors.append({"subquery": name, "kind": "timeout"})
            return None
        except Exception as e:
            errors.append({"subquery": name, "kind": "error", "message": str(e)})
            return None

    node_data, neighbors_raw, chain_items = await asyncio.gather(
        _safe(
            "node",
            fetch_node_by_id(
                conn,
                node_id,
                workspace_id=graph_workspace_id,
                visible_projects=visible_projects,
            ),
        ),
        _safe(
            "neighbors",
            fetch_active_neighbors(
                conn,
                node_id,
                limit=limit,
                workspace_id=graph_workspace_id,
                visible_projects=visible_projects,
            ),
        ),
        _safe(
            "chain",
            _fetch_context_chain(
                conn,
                node_id,
                limit,
                graph_workspace_id,
                visible_projects,
            ),
        ),
    )

    project_id: str | None = (node_data or {}).get("project_id")
    learnings = await _query_learnings(
        conn,
        project_id,
        limit,
        workspace_id,
        visible_projects,
    )

    neighbors_raw = neighbors_raw or []
    chain_items = chain_items or []

    # Rank neighbors using the project_id from the node
    ranked_neighbors = await rank_neighbors(
        conn,
        neighbors_raw,
        project_id=project_id or "",
        top_k=limit,
        workspace_id=graph_workspace_id,
        visible_projects=visible_projects,
    )

    return {
        "neighbors": ranked_neighbors,
        "context_chain": chain_items,
        "applicable_learnings": learnings,
        "meta": _make_meta(ranked_neighbors, chain_items, learnings, errors, deep_effective=deep, deep_default_source="client"),
    }


async def build_kg_context_for_task(
    conn: aiosqlite.Connection,
    task_id: str,
    deep: bool = False,
    *,
    ctx: CallerContext,
    visible_projects: set[str] | None,
) -> dict[str, Any]:
    """KG context bundle for a task node.

    Accepts either a raw UUID or a fully prefixed ``task:artifact:{uuid}`` id.
    Prefixing is idempotent so callers can pass whichever form they have.
    """
    workspace_id, visible_projects = require_kg_visibility(ctx, visible_projects)
    node_id = (
        task_id
        if task_id.startswith("task:artifact:")
        else f"task:artifact:{task_id}"
    )
    return await _build_kg_context_for_node(
        conn,
        node_id,
        deep,
        workspace_id=workspace_id,
        visible_projects=visible_projects,
    )


async def build_kg_context_for_handoff(
    conn: aiosqlite.Connection,
    handoff_id: str,
    deep: bool = False,
    *,
    ctx: CallerContext,
    visible_projects: set[str] | None,
) -> dict[str, Any]:
    """KG context bundle for a handoff node.

    Accepts any of:
    - fully prefixed ``handoff:artifact:{stem}`` → passthrough
    - bare stem (e.g. ``2026-04-16-foo``) → prefixed
    - filename with ``handoff-`` prefix and/or ``.md`` suffix → stripped and
      prefixed

    Prefixing is idempotent.
    """
    workspace_id, visible_projects = require_kg_visibility(ctx, visible_projects)
    if handoff_id.startswith("handoff:artifact:"):
        node_id = handoff_id
    else:
        stem = handoff_id
        if stem.endswith(".md"):
            stem = stem[:-3]
        if stem.startswith("handoff-"):
            stem = stem[len("handoff-"):]
        node_id = f"handoff:artifact:{stem}"
    return await _build_kg_context_for_node(
        conn,
        node_id,
        deep,
        workspace_id=workspace_id,
        visible_projects=visible_projects,
    )


async def build_kg_context_for_learning(
    conn: aiosqlite.Connection,
    learning_id: str,
    deep: bool = False,
    *,
    ctx: CallerContext,
    visible_projects: set[str] | None,
) -> dict[str, Any]:
    """KG context bundle for a learning node.

    Accepts either a raw UUID or a fully prefixed
    ``learning:artifact:{uuid}`` id. Prefixing is idempotent.
    """
    workspace_id, visible_projects = require_kg_visibility(ctx, visible_projects)
    node_id = (
        learning_id
        if learning_id.startswith("learning:artifact:")
        else f"learning:artifact:{learning_id}"
    )
    return await _build_kg_context_for_node(
        conn,
        node_id,
        deep,
        workspace_id=workspace_id,
        visible_projects=visible_projects,
    )


async def build_kg_context_for_pr(
    conn: aiosqlite.Connection,
    task_id: str,
    deep: bool = False,
    *,
    ctx: CallerContext,
    visible_projects: set[str] | None,
) -> dict[str, Any]:
    """KG context bundle for a PR node (addressed via its task_id).

    Uses the ``task:artifact:`` prefix, not ``pr:artifact:``. The task node
    offers richer context and already includes the PR as a neighbor via the
    ``produces`` relation. Prefixing is idempotent.
    """
    workspace_id, visible_projects = require_kg_visibility(ctx, visible_projects)
    node_id = (
        task_id
        if task_id.startswith("task:artifact:")
        else f"task:artifact:{task_id}"
    )
    return await _build_kg_context_for_node(
        conn,
        node_id,
        deep,
        workspace_id=workspace_id,
        visible_projects=visible_projects,
    )


# ---------------------------------------------------------------------------
# Aggregate builder — for list endpoints (up to 10 nodes)
# ---------------------------------------------------------------------------


async def build_kg_context_aggregate(
    conn: aiosqlite.Connection,
    node_ids: list[str],
    project_id: str,
    deep: bool = False,
    *,
    ctx: CallerContext,
    visible_projects: set[str] | None,
) -> dict[str, Any]:
    """Aggregate KG context for multiple nodes (max 10).

    Runs build_kg_context_for_task for each node_id in parallel, then
    de-duplicates neighbors by id and merges learnings by id.
    """
    workspace_id, visible_projects = require_kg_visibility(ctx, visible_projects)
    if visible_projects is not None and project_id not in visible_projects:
        return {
            "nodes_processed": 0,
            "neighbors": [],
            "applicable_learnings": [],
            "meta": {
                "ranker_version": RANKER_VERSION,
                "item_count": 0,
                "truncated": len(node_ids) > 10,
                "errors": [],
            },
        }
    capped_ids = node_ids[:10]

    results = await asyncio.gather(
        *[
            _build_kg_context_for_node(
                conn,
                nid,
                deep,
                workspace_id=workspace_id,
                visible_projects=visible_projects,
            )
            for nid in capped_ids
        ],
        return_exceptions=True,
    )

    # Merge: de-duplicate neighbors by id, learnings by id
    seen_neighbor_ids: set[str] = set()
    merged_neighbors: list[dict] = []
    seen_learning_ids: set[str] = set()
    merged_learnings: list[dict] = []
    all_errors: list[dict] = []

    for r in results:
        if isinstance(r, Exception):
            all_errors.append({"kind": "error", "message": str(r)})
            continue
        for n in r.get("neighbors", []):
            nid = n.get("id")
            if nid and nid not in seen_neighbor_ids:
                seen_neighbor_ids.add(nid)
                merged_neighbors.append(n)
        for lrn in r.get("applicable_learnings", []):
            lid = lrn.get("id")
            if lid and lid not in seen_learning_ids:
                seen_learning_ids.add(lid)
                merged_learnings.append(lrn)
        all_errors.extend(r.get("meta", {}).get("errors", []))

    return {
        "nodes_processed": len(capped_ids),
        "neighbors": merged_neighbors,
        "applicable_learnings": merged_learnings,
        "meta": {
            "ranker_version": RANKER_VERSION,
            "item_count": len(merged_neighbors) + len(merged_learnings),
            "truncated": len(node_ids) > 10,
            "errors": all_errors,
        },
    }
