# v1.0.0 - 2026-04-16 - kg primitive layer (Phase 7.0 Commit 0)
"""Primitive KG queries centralising active-node/active-edge guards.

Thin async helpers over ``graph_nodes`` / ``graph_edges``. Every primitive
applies the standard live-view filters (``deprecated_at IS NULL`` on nodes,
``valid_until IS NULL`` on edges) so callers cannot forget them. Composition
(parallelism, ranking, degrade-to-empty) is a caller concern.
"""
from __future__ import annotations

from typing import Any, Iterable, Optional

import aiosqlite


_VALID_DIRECTIONS = ("outgoing", "incoming")
_HOTSPOT_WINDOW_COLUMNS = {7: "touch_count_7d", 30: "touch_count_30d"}


def _project_visibility_clause(
    column: str,
    visible_projects: set[str] | None,
) -> tuple[str, list[str]]:
    """Return a deterministic SQL predicate for the caller's project grants."""
    if visible_projects is None:
        return "", []
    if not visible_projects:
        return " AND 0", []
    ordered = sorted(visible_projects)
    placeholders = ",".join("?" for _ in ordered)
    return f" AND {column} IN ({placeholders})", ordered


def _unambiguous_workspace_project_clause(
    column: str,
    workspace_id: str | None,
    *,
    alias: str,
) -> tuple[str, list[str]]:
    """Fail closed while graph rows have no durable ``workspace_id``.

    A remote graph row is usable only when its project slug is owned by the
    authenticated workspace and by no other workspace. Same-slug bindings are
    quarantined until graph nodes/edges become workspace-keyed and are rebuilt.
    ``workspace_id=None`` is the explicit local single-user compatibility path.
    """
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


async def fetch_active_neighbors(
    conn: aiosqlite.Connection,
    node_id: str,
    direction: str = "outgoing",
    edge_types: Optional[Iterable[str]] = None,
    limit: int = 10,
    *,
    workspace_id: str | None = None,
    visible_projects: set[str] | None = None,
) -> list[dict]:
    """Return live neighbors of ``node_id`` joined on active edges."""
    if direction not in _VALID_DIRECTIONS:
        raise ValueError(f"direction must be one of {_VALID_DIRECTIONS}, got {direction!r}")

    if direction == "outgoing":
        # node_id is the source; neighbor is the target
        node_join_col = "e.target_id"
        anchor_col = "e.source_id"
    else:
        # node_id is the target; neighbor is the source
        node_join_col = "e.source_id"
        anchor_col = "e.target_id"

    params: list[Any] = [node_id]
    relation_clause = ""
    if edge_types is not None:
        edge_types_list = list(edge_types)
        if not edge_types_list:
            return []
        placeholders = ",".join("?" for _ in edge_types_list)
        relation_clause = f" AND e.relation IN ({placeholders})"
        params.extend(edge_types_list)
    anchor_workspace_clause, anchor_workspace_params = (
        _unambiguous_workspace_project_clause(
            "anchor.project_id", workspace_id, alias="neighbor_anchor"
        )
    )
    neighbor_workspace_clause, neighbor_workspace_params = (
        _unambiguous_workspace_project_clause(
            "n.project_id", workspace_id, alias="neighbor_target"
        )
    )
    params.extend(anchor_workspace_params)
    params.extend(neighbor_workspace_params)
    anchor_visibility_clause = ""
    anchor_visibility_params: list[str] = []
    if visible_projects is not None:
        if not visible_projects:
            return []
        anchor_visibility_params = sorted(visible_projects)
        placeholders = ",".join("?" for _ in anchor_visibility_params)
        anchor_visibility_clause = (
            " AND EXISTS (SELECT 1 FROM graph_nodes visible_anchor "
            f"WHERE visible_anchor.id = {anchor_col} "
            f"AND visible_anchor.project_id IN ({placeholders}))"
        )
    neighbor_visibility_clause, neighbor_visibility_params = _project_visibility_clause(
        "n.project_id", visible_projects
    )
    params.extend(anchor_visibility_params)
    params.extend(neighbor_visibility_params)
    params.append(limit)

    sql = (
        "SELECT n.id, n.type, n.name, n.qualified_name, n.file_path, n.project_id, "
        "e.relation, e.confidence "
        "FROM graph_edges e "
        f"JOIN graph_nodes n ON n.id = {node_join_col} "
        f"JOIN graph_nodes anchor ON anchor.id = {anchor_col} "
        f"WHERE {anchor_col} = ? "
        "AND e.valid_until IS NULL "
        "AND n.deprecated_at IS NULL"
        f"{relation_clause} "
        f"{anchor_workspace_clause} "
        f"{neighbor_workspace_clause} "
        f"{anchor_visibility_clause} "
        f"{neighbor_visibility_clause} "
        "ORDER BY n.qualified_name "
        "LIMIT ?"
    )
    cur = await conn.execute(sql, params)
    return [dict(r) async for r in cur]


async def fetch_active_nodes_by_project(
    conn: aiosqlite.Connection,
    project_id: str,
    type_filter: Optional[str] = None,
    order_by: str = "last_seen_at DESC NULLS LAST",
    limit: int = 15,
    *,
    workspace_id: str | None = None,
    visible_projects: set[str] | None = None,
) -> list[dict]:
    """Return live nodes scoped to a project, optionally filtered by type."""
    params: list[Any] = [project_id]
    type_clause = ""
    if type_filter is not None:
        type_clause = " AND type = ?"
        params.append(type_filter)
    workspace_clause, workspace_params = _unambiguous_workspace_project_clause(
        "graph_nodes.project_id", workspace_id, alias="project_nodes"
    )
    params.extend(workspace_params)
    visibility_clause, visibility_params = _project_visibility_clause(
        "graph_nodes.project_id", visible_projects
    )
    params.extend(visibility_params)
    params.append(limit)

    sql = (
        "SELECT id, type, name, qualified_name, last_seen_at, project_id "
        "FROM graph_nodes "
        "WHERE deprecated_at IS NULL AND project_id = ?"
        f"{type_clause} "
        f"{workspace_clause} "
        f"{visibility_clause} "
        f"ORDER BY {order_by} "
        "LIMIT ?"
    )
    cur = await conn.execute(sql, params)
    return [dict(r) async for r in cur]


async def fetch_hotspot_nodes(
    conn: aiosqlite.Connection,
    project_id: str,
    window_days: int = 30,
    limit: int = 15,
    *,
    workspace_id: str | None = None,
    visible_projects: set[str] | None = None,
) -> list[dict]:
    """Return top hotspot function/file nodes by touch count in the window."""
    column = _HOTSPOT_WINDOW_COLUMNS.get(window_days)
    if column is None:
        raise ValueError(
            f"window_days must be one of {sorted(_HOTSPOT_WINDOW_COLUMNS)}, got {window_days}"
        )

    params: list[Any] = [project_id]
    workspace_clause, workspace_params = _unambiguous_workspace_project_clause(
        "graph_nodes.project_id", workspace_id, alias="hotspot"
    )
    params.extend(workspace_params)
    visibility_clause, visibility_params = _project_visibility_clause(
        "graph_nodes.project_id", visible_projects
    )
    params.extend(visibility_params)
    params.append(limit)
    sql = (
        f"SELECT id, type, name, qualified_name, {column} AS touch_count "
        "FROM graph_nodes "
        "WHERE deprecated_at IS NULL "
        "AND project_id = ? "
        "AND type IN ('function','file') "
        f"{workspace_clause}"
        f"{visibility_clause} "
        f"ORDER BY {column} DESC "
        "LIMIT ?"
    )
    cur = await conn.execute(sql, params)
    return [dict(r) async for r in cur]


async def fetch_cross_project_mentions(
    conn: aiosqlite.Connection,
    project_id: str,
    limit: int = 10,
    *,
    workspace_id: str | None = None,
    visible_projects: set[str] | None = None,
) -> list[dict]:
    """Return nodes in OTHER projects linked from this project's nodes."""
    if visible_projects is not None and project_id not in visible_projects:
        return []
    params: list[Any] = [project_id, project_id]
    source_workspace_clause, source_workspace_params = (
        _unambiguous_workspace_project_clause(
            "n1.project_id", workspace_id, alias="cross_source"
        )
    )
    target_workspace_clause, target_workspace_params = (
        _unambiguous_workspace_project_clause(
            "n2.project_id", workspace_id, alias="cross_target"
        )
    )
    params.extend(source_workspace_params)
    params.extend(target_workspace_params)
    visibility_clause, visibility_params = _project_visibility_clause(
        "n2.project_id", visible_projects
    )
    params.extend(visibility_params)
    params.append(limit)
    sql = (
        "SELECT DISTINCT n2.id, n2.type, n2.name, n2.project_id "
        "FROM graph_edges e "
        "JOIN graph_nodes n1 ON e.source_id = n1.id "
        "JOIN graph_nodes n2 ON e.target_id = n2.id "
        "WHERE e.valid_until IS NULL "
        "AND n1.project_id = ? "
        "AND n2.project_id IS NOT NULL "
        "AND n2.project_id != ? "
        f"{source_workspace_clause}"
        f"{target_workspace_clause}"
        f"{visibility_clause} "
        "ORDER BY n2.last_seen_at DESC NULLS LAST "
        "LIMIT ?"
    )
    cur = await conn.execute(sql, params)
    return [dict(r) async for r in cur]


async def fetch_node_by_id(
    conn: aiosqlite.Connection,
    node_id: str,
    *,
    workspace_id: str | None = None,
    visible_projects: set[str] | None = None,
) -> Optional[dict]:
    """Return the live node row for ``node_id`` or ``None`` if absent/deprecated."""
    params: list[Any] = [node_id]
    workspace_clause, workspace_params = _unambiguous_workspace_project_clause(
        "graph_nodes.project_id", workspace_id, alias="node_lookup"
    )
    params.extend(workspace_params)
    visibility_clause, visibility_params = _project_visibility_clause(
        "graph_nodes.project_id", visible_projects
    )
    params.extend(visibility_params)
    cur = await conn.execute(
        "SELECT * FROM graph_nodes WHERE deprecated_at IS NULL AND id = ?"
        + workspace_clause
        + visibility_clause,
        params,
    )
    row = await cur.fetchone()
    return dict(row) if row is not None else None
