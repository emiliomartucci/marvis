# v1.0.0 - 2026-08-05 - Plan 2 U1: graph ingest use_case (fastapi-free)
"""Ingest a parsed graph batch for one project, atomically.

Plan 2 ("code-graph senza codice sul tenant"): the tenant receives nodes/edges
parsed elsewhere and NEVER the source. This use_case owns the atomic-replace
semantics and the provenance ledger.

Atomicity (why one transaction, not the graph writer's per-chunk BEGIN):
    ``core.scripts._graph_writer`` opens its own ``BEGIN IMMEDIATE`` per chunk,
    which is fine for incremental upserts but would let a reader observe an
    empty project graph between DELETE and INSERT. Ingest is a full generation
    swap, so the DELETE(project) + INSERT(batch) + provenance upsert run inside
    ONE ``BEGIN IMMEDIATE``: a concurrent reader sees either the entire previous
    graph or the entire new one, never a half or empty state (learning
    ea89bac4). A malformed batch (a node type / edge relation the DB CHECK
    rejects) rolls the whole thing back, so a bad ingest cannot wipe the
    project's existing graph.

Node/edge column order matches ``_graph_writer`` exactly; the ON CONFLICT
clauses resolve intra-batch duplicates (the parser emits stub target nodes that
collide with the real definition across files — real wins via COALESCE).
"""
from __future__ import annotations

import json

import aiosqlite

from core.api.models.graph_ingest import GraphIngestRequest
from core.api.services import access_grants
from core.api.use_cases._context import (
    CallerContext,
    require_role_ctx,
    require_workspace_ctx,
)
from core.api.use_cases._errors import NotFoundError, ValidationError

_SQLITE_BIND_CHUNK = 400

_NODE_SQL = """
    INSERT INTO graph_nodes
        (id, type, name, qualified_name, file_path, line_number,
         metadata, last_seen_at, project_id)
    VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'), ?)
    ON CONFLICT(id) DO UPDATE SET
        type = excluded.type,
        name = excluded.name,
        qualified_name = excluded.qualified_name,
        file_path = COALESCE(excluded.file_path, graph_nodes.file_path),
        line_number = COALESCE(excluded.line_number, graph_nodes.line_number),
        metadata = excluded.metadata,
        last_seen_at = datetime('now'),
        updated_at = datetime('now'),
        project_id = excluded.project_id
"""

# Edges: DO NOTHING on the (source, target, relation) unique key. After the
# per-project DELETE the only possible conflict is an intra-batch duplicate, so
# keeping the first occurrence is correct and deterministic.
_EDGE_SQL = """
    INSERT INTO graph_edges
        (source_id, target_id, relation, confidence, source,
         metadata, source_file, source_line, project_id)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(source_id, target_id, relation) DO NOTHING
"""

_PROVENANCE_SQL = """
    INSERT INTO graph_ingest_provenance
        (project_id, source, commit_sha, dirty, parser_version,
         node_count, edge_count, generated_at, ingested_at)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
    ON CONFLICT(project_id) DO UPDATE SET
        source = excluded.source,
        commit_sha = excluded.commit_sha,
        dirty = excluded.dirty,
        parser_version = excluded.parser_version,
        node_count = excluded.node_count,
        edge_count = excluded.edge_count,
        generated_at = excluded.generated_at,
        ingested_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
"""


def _dumps(meta: object) -> str:
    try:
        return json.dumps(meta, separators=(",", ":"), sort_keys=True)
    except (TypeError, ValueError):
        return "{}"


def _is_local_single_user(ctx: CallerContext) -> bool:
    """Local data-plane compatibility, independent of approval authority."""
    return ctx.is_local_os_account


async def _require_project_access(
    ctx: CallerContext,
    db: aiosqlite.Connection,
    project: str,
    *,
    write: bool,
) -> str:
    """Require exact workspace access and quarantine shared legacy slugs.

    The graph schema still keys nodes/provenance by a global ``project_id``.
    Until that storage is workspace-keyed, a slug owned by more than one
    workspace cannot be represented safely and must remain non-enumerable.
    Legacy/local single-user databases keep their established behavior.
    """

    workspace_id = require_workspace_ctx(ctx)
    if _is_local_single_user(ctx):
        return workspace_id
    if not await access_grants.workspace_isolation_enabled(db):
        return workspace_id

    allowed = (
        await access_grants.can_write_project(db, ctx, project)
        if write
        else await access_grants.can_read_project(db, ctx, project)
    )
    if not allowed:
        raise NotFoundError(code="project_not_found", message="Not found")

    try:
        rows = await (
            await db.execute(
                "SELECT workspace_id FROM workspace_projects "
                "WHERE project_slug = ?",
                (project,),
            )
        ).fetchall()
    except aiosqlite.Error:
        rows = []
    owners = {str(row[0]) for row in rows if row[0]}
    if owners != {workspace_id}:
        raise NotFoundError(code="project_not_found", message="Not found")
    return workspace_id


async def _assert_batch_is_project_confined(
    db: aiosqlite.Connection,
    *,
    project: str,
    node_ids: set[str],
    edge_endpoints: set[str],
) -> None:
    """Reject a batch that could overwrite or cascade-delete another graph."""

    if not edge_endpoints.issubset(node_ids):
        raise ValidationError(
            code="graph_ingest_edge_outside_batch",
            message="graph batch edges must reference nodes in the same batch",
        )

    ordered_ids = sorted(node_ids)
    for offset in range(0, len(ordered_ids), _SQLITE_BIND_CHUNK):
        chunk = ordered_ids[offset : offset + _SQLITE_BIND_CHUNK]
        placeholders = ",".join("?" for _ in chunk)
        row = await (
            await db.execute(
                "SELECT 1 FROM graph_nodes "
                f"WHERE id IN ({placeholders}) "
                "AND (project_id IS NULL OR project_id <> ?) LIMIT 1",
                [*chunk, project],
            )
        ).fetchone()
        if row is not None:
            raise ValidationError(
                code="graph_ingest_node_id_conflict",
                message="graph batch conflicts with another project's node ids",
            )

    # Replacing a project's nodes cascades through every incident edge. Refuse
    # the swap if a legacy/foreign edge would be deleted as collateral damage.
    foreign_edge = await (
        await db.execute(
            """
            SELECT 1
            FROM graph_edges e
            JOIN graph_nodes src ON src.id = e.source_id
            JOIN graph_nodes dst ON dst.id = e.target_id
            WHERE (src.project_id = ? OR dst.project_id = ?)
              AND (e.project_id IS NULL OR e.project_id <> ?)
            LIMIT 1
            """,
            (project, project, project),
        )
    ).fetchone()
    if foreign_edge is not None:
        raise ValidationError(
            code="graph_ingest_cross_project_edge",
            message="project graph has a foreign edge and cannot be replaced safely",
        )


async def ingest_graph(
    ctx: CallerContext,
    db: aiosqlite.Connection,
    request: GraphIngestRequest,
) -> dict:
    """Atomically replace one project's graph and record its provenance.

    Requires operator+ (agents are operator by default). Returns a summary; the
    ingested graph is never visible half-written.
    """
    require_role_ctx(ctx, "operator")

    project = request.project
    prov = request.provenance
    await _require_project_access(ctx, db, project, write=True)

    node_rows = [
        (
            n.id,
            n.type,
            n.name,
            n.qualified_name,
            n.file_path,
            n.line_number,
            _dumps(n.metadata),
            project,
        )
        for n in request.nodes
    ]
    edge_rows = [
        (
            e.source_id,
            e.target_id,
            e.relation,
            float(e.confidence),
            e.source,
            _dumps(e.metadata),
            e.source_file,
            e.source_line,
            project,
        )
        for e in request.edges
    ]

    node_ids = {node.id for node in request.nodes}
    edge_endpoints = {
        endpoint
        for edge in request.edges
        for endpoint in (edge.source_id, edge.target_id)
    }

    await db.execute("BEGIN IMMEDIATE")
    try:
        await _assert_batch_is_project_confined(
            db,
            project=project,
            node_ids=node_ids,
            edge_endpoints=edge_endpoints,
        )
        # Order matters: edges first (FK children), then nodes — but on DELETE
        # the FK is ON DELETE CASCADE, so deleting nodes removes their edges.
        # Delete edges explicitly first anyway to be robust if FK enforcement
        # is off in a given runtime.
        await db.execute("DELETE FROM graph_edges WHERE project_id = ?", (project,))
        await db.execute("DELETE FROM graph_nodes WHERE project_id = ?", (project,))
        # Insert nodes BEFORE edges so edge FK targets already exist (the batch
        # is self-consistent: the parser emits a stub node for every call/import
        # target).
        await db.executemany(_NODE_SQL, node_rows)
        if edge_rows:
            await db.executemany(_EDGE_SQL, edge_rows)
        await db.execute(
            _PROVENANCE_SQL,
            (
                project,
                prov.source,
                prov.commit_sha,
                1 if prov.dirty else 0,
                prov.parser_version,
                len(node_rows),
                len(edge_rows),
                prov.generated_at,
            ),
        )
        await db.commit()
    except aiosqlite.IntegrityError as exc:
        await db.rollback()
        # A rejected node type / edge relation / FK violation lands here. The
        # project's prior graph is intact (nothing committed).
        raise ValidationError(
            code="graph_ingest_rejected",
            message=f"graph batch rejected, project graph unchanged: {exc}",
        ) from exc
    except Exception:
        await db.rollback()
        raise

    return {
        "activated": True,
        "project": project,
        "source": prov.source,
        "commit_sha": prov.commit_sha,
        "dirty": prov.dirty,
        "nodes": len(node_rows),
        "edges": len(edge_rows),
    }


async def read_provenance(
    ctx: CallerContext,
    db: aiosqlite.Connection,
    project: str,
) -> dict:
    """Return the recorded provenance for a project's active graph (viewer+)."""
    require_role_ctx(ctx, "viewer")
    await _require_project_access(ctx, db, project, write=False)

    cur = await db.execute(
        """
        SELECT project_id, source, commit_sha, dirty, parser_version,
               node_count, edge_count, generated_at, ingested_at
        FROM graph_ingest_provenance
        WHERE project_id = ?
        """,
        (project,),
    )
    row = await cur.fetchone()
    if row is None:
        raise NotFoundError(
            code="graph_provenance_not_found",
            message=f"no graph provenance recorded for project '{project}'",
        )

    return {
        "project": row["project_id"],
        "source": row["source"],
        "commit_sha": row["commit_sha"],
        "dirty": bool(row["dirty"]),
        "parser_version": row["parser_version"],
        "node_count": row["node_count"],
        "edge_count": row["edge_count"],
        "generated_at": row["generated_at"],
        "ingested_at": row["ingested_at"],
    }
