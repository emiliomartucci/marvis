# v1.8.0 - 2026-04-17 - KG bug fix: sync_task_to_graph helper (event-driven task node UPSERT on POST /tasks)
# v1.7.0 - 2026-04-14 - KG Fase 2: cross-project project_id + edge_types + undirected_neighbors + BFS cycle detection
# v1.6.0 - 2026-04-14 - KG Fase 1f: graph_impact + graph_context + graph_pattern
"""Knowledge Graph service — async functions, no classes (pattern MarvisX,
mirrors api/services/embedding_service.py).

Backed by graph_nodes / graph_edges (migration 065 + 066 + 067 + 068). Used by
the GET /api/v1/graph/neighbors/{node_id} endpoint, GET /api/v1/graph/hotspots,
GET /api/v1/graph/impact, /context, /pattern, and the MCP tools
`graph_neighbors`, `graph_hotspots`, `graph_impact`, `graph_context`,
`graph_pattern`.

Node ID format (Fase 1a):
    {lang}:{type}:{qualified_name}
    e.g. "py:function:api.db.get_write_db"
         "ts:function:console.src.components.modal.open"
         "ts:file:console.src.app.page"

Legacy spike ids without prefix (e.g. "function:api.db.get_db") are migrated
to "py:" via scripts/migrate_spike_node_ids.py (one-shot, pre-backfill).

Security:
- node_id MUST match NODE_ID_PATTERN (defense in depth on the regex)
- limit clamped to [1, 200] regardless of caller
- as_of (Fase 1d) MUST match ISO_TIMESTAMP_PATTERN (anti-injection)

Temporal filtering (Fase 1d):
- default (`as_of=None`): exclude nodes with `deprecated_at IS NOT NULL`
  and edges with `valid_until IS NOT NULL`
- time-travel (`as_of=<iso>`): include historical state, i.e. nodes that
  existed at `as_of` (created_at <= as_of AND (deprecated_at IS NULL OR
  deprecated_at > as_of)) and edges that were valid at `as_of`
  (first_seen_at <= as_of AND (valid_until IS NULL OR valid_until > as_of))
"""
from __future__ import annotations

import json
import re
from typing import Any, Literal

import aiosqlite

# Mirrors the pattern used by the MCP tool zod schema and the endpoint Query
# regex. Keep the three in sync (graph_service.py, scripts/ast_parser.py,
# mcp-pir/index.mjs).
# Fase 1a: prefix py|ts mandatory + kebab-case dash support for TS filenames.
# Fase 1c: extended for artifact prefixes (task|pr|commit|handoff|solution|learning)
# with `artifact` as the only allowed sub-type so we don't have to special-case
# the existing function|file|module set on artifact ids.
# Fase 1h: added doc-type prefixes (audit|spike|analysis|research|rubric|guide|mockup)
# so populate_artifacts can index every docs/ subdir as a distinct node type.
# Fase 2: added `project` prefix (cross-project hub nodes, target of `mentions`)
# and `file` prefix (on-demand file-artifact nodes per PAT-8; id format
# `file:artifact:{sha256(abs_path)[:12]}`).
# Fase 2.z: added `hook|skill|command|plugin` prefixes for `.claude/` infra
# indexing. PAT AM-03: kind=`artifact` with deterministic slug = filename stem
# (verbose ma query-friendly: `hook:artifact:quality-gate` non sha256).
# Phase 6: added `plan|brainstorm` doc-type prefixes per migration 077. Questi
# coprono docs/plans/ e docs/brainstorms/ nei progetti metadata-only
# (--all-projects scope) cosi' il KG indicizza anche piani e brainstorm.
# Phase 7.x hygiene (plan Pilastro 5): single source of truth for node taxonomy.
# NODE_ID_PATTERN is DERIVED from these tuples. All MCP/docs/enum usage must
# read from these (avoid re-listing, see kieran-python deepen section 5).
NODE_KINDS: tuple[str, ...] = (
    "function", "file", "module", "artifact", "sheet",
)
NODE_PREFIXES: tuple[str, ...] = (
    "py", "ts", "task", "pr", "commit", "handoff", "solution", "learning",
    "audit", "spike", "analysis", "research", "rubric", "guide", "mockup",
    "project", "file", "hook", "skill", "command", "plugin",
    "plan", "brainstorm",
    "inbox",  # Phase 7.3 (migration 090): saved inbox_items indexed via scripts/populate_inbox_nodes.py
    "xlsx",  # Universal ingestion E4.2: workbook + sheet artifact nodes.
    # Phase 1.5 E5-fix9 + fix13: business document taxonomy. spike/rubric kept
    # in regex per backward compat (legacy node queryability) ma deprecati per
    # nuovi insert via _NODE_TYPE_ALLOWLIST (insert_saga.py) + CHECK constraint
    # (migration 098 graph_nodes.type).
    # Migration 125 adds record for factual/admin documents.
    "policy", "contract", "transcript", "record", "report",
)  # 30 prefixes — matches NODE_ID_PATTERN regex (post-migration 125)

# NODE_ID_PATTERN DERIVED from tuples (single source)
NODE_ID_PATTERN = re.compile(
    rf"^({'|'.join(NODE_PREFIXES)}):"
    rf"({'|'.join(NODE_KINDS)}):[a-zA-Z0-9_\-.]+$"
)
MAX_NODE_ID_LEN = 256
MAX_NEIGHBORS = 200

# Fase 1d: accepted formats — `YYYY-MM-DD` or `YYYY-MM-DD HH:MM:SS` or full ISO
# (with optional `T`, optional trailing `Z`, optional fractional seconds). The
# pattern is a subset of ISO 8601 sufficient for `datetime('<as_of>')` in
# SQLite. We reject anything else before concatenating it into SQL (defense
# in depth, even though we always pass as_of via bind params below).
ISO_TIMESTAMP_PATTERN = re.compile(
    r"^\d{4}-\d{2}-\d{2}"
    r"([ T]\d{2}:\d{2}:\d{2}(\.\d+)?)?"
    r"Z?$"
)

Direction = Literal["incoming", "outgoing", "both"]

# Full edge-type catalog. Keep in sync with the latest
# migrations/*_kg_*.sql CHECK(relation) (see scripts/_drift_check.py check B
# for the canonical 6-source sync mandate).
# `symmetric_edges` → relation is undirected in practice; helper
# `undirected_neighbors` queries BOTH sides to preserve intent (ARCH-02).
EDGE_TYPES: tuple[str, ...] = (
    "calls", "imports", "defines",
    "produces", "contains",
    "describes", "documents", "cites", "applies_to",
    "depends_on", "mentions", "refers_to", "shares_tag", "similar_to",
    "resolves_to",  # Phase 7.2: module stub -> file canonical bridge
    "modifies",  # KG PR-Impact (mig 132): pr_artifact -> function_artifact
)
SYMMETRIC_EDGES: frozenset[str] = frozenset({"shares_tag", "similar_to"})

# Fase 2: project slug constraints (ARCH-01). Matches the Query pattern in
# api/routers/graph.py and the zod regex in mcp-pir/index.mjs.
PROJECT_SLUG_PATTERN = re.compile(r"^[a-z0-9][a-z0-9&\-]+$")
MAX_PROJECT_SLUG_LEN = 50

# Fase 2 BFS cycle detection (DI-A9/PERF-7): multi-hop walks protect against
# cycle explosions with visited set + depth cap + node cap.
MAX_MULTI_HOP_DEPTH: int = 4
MAX_MULTI_HOP_NODES_VISITED: int = 500


def validate_project(project: str | None) -> str | None:
    """Reject malformed project slugs. Returns the original on success.

    None passes through (no filter).
    """
    if project is None:
        return None
    if not isinstance(project, str) or not project:
        raise ValueError(
            "project must be a non-empty slug. "
            f"Expected format: {PROJECT_SLUG_PATTERN.pattern} "
            "(lowercase alphanumeric + hyphens + '&', starting with [a-z0-9]). "
            "Example: 'marvisx', 'pir-api', 'docs-bot'. "
            "Fix: pass project=<slug> or omit to query all projects."
        )
    if len(project) > MAX_PROJECT_SLUG_LEN:
        raise ValueError(
            f"project slug too long ({len(project)} > {MAX_PROJECT_SLUG_LEN}). "
            f"Got: {project!r}. "
            f"Fix: use a valid slug from mcp__pir__list_projects (all slugs are ≤{MAX_PROJECT_SLUG_LEN} chars)."
        )
    if not PROJECT_SLUG_PATTERN.match(project):
        raise ValueError(
            f"Invalid project slug: {project!r}. "
            f"Expected pattern: {PROJECT_SLUG_PATTERN.pattern} "
            "(lowercase alphanumeric + hyphens + '&'). "
            "Fix: check spelling with mcp__pir__list_projects; slugs are case-sensitive and must start with [a-z0-9]."
        )
    return project


def validate_edge_types(edge_types: list[str] | tuple[str, ...] | None) -> list[str] | None:
    """Reject unknown edge relations. Returns the original on success.

    None or empty passes through (no filter).
    """
    if edge_types is None:
        return None
    if not isinstance(edge_types, (list, tuple)):
        raise ValueError(
            f"edge_types must be a list/tuple, got {type(edge_types).__name__}. "
            f"Fix: pass edge_types=['calls', 'imports'] or omit to use the default Fase-1 set {{calls,imports,defines}}."
        )
    if not edge_types:
        return None
    unknown = [e for e in edge_types if e not in EDGE_TYPES]
    if unknown:
        raise ValueError(
            f"Unknown edge_types: {unknown!r}. "
            f"Allowed values: {list(EDGE_TYPES)}. "
            "Fix: use one of the allowed relations. "
            f"Symmetric relations (treated undirected): {sorted(SYMMETRIC_EDGES)}."
        )
    return list(edge_types)


def validate_node_id(node_id: str) -> str:
    """Reject malformed or oversized ids. Returns the original on success."""
    if not isinstance(node_id, str) or not node_id:
        raise ValueError(
            "node_id is required. "
            "Expected format: '{lang}:{type}:{qualified_name}' "
            "(e.g. 'py:function:api.db.get_write_db', 'ts:file:console.src.app.page'). "
            "Fix: pass a valid node_id string — find one via mcp__pir__graph_neighbors or SELECT id FROM graph_nodes."
        )
    if len(node_id) > MAX_NODE_ID_LEN:
        raise ValueError(
            f"node_id too long ({len(node_id)} > {MAX_NODE_ID_LEN}): {node_id[:80]!r}... "
            "Fix: qualified_name must be under the limit; check for runaway nesting or accidentally pasted content."
        )
    if not NODE_ID_PATTERN.match(node_id):
        raise ValueError(
            f"Invalid node_id format: {node_id!r}. "
            "Expected: '{lang}:{type}:{qualified_name}' where "
            "lang ∈ {py, ts, task, pr, commit, handoff, solution, learning, audit, spike, analysis, "
            "research, rubric, guide, mockup, project, file, hook, skill, command, plugin}, "
            "type ∈ {function, file, module, artifact}, "
            "qualified_name matches [a-zA-Z0-9_\\-.]+. "
            "Examples: 'py:function:api.db.get_db', 'ts:file:console.src.app.page', 'task:artifact:abc-123-def'. "
            "Fix: check prefix+type+slug, or use mcp__pir__search to find the exact id."
        )
    return node_id


def validate_as_of(as_of: str | None) -> str | None:
    """Reject malformed ISO timestamps. Returns the original on success.

    None passes through (default = "now" semantics at query time, i.e. exclude
    deprecated rows).
    """
    if as_of is None:
        return None
    if not isinstance(as_of, str) or not as_of:
        raise ValueError(
            "as_of must be a non-empty ISO 8601 timestamp. "
            "Accepted formats: 'YYYY-MM-DD', 'YYYY-MM-DD HH:MM:SS', or full ISO with optional 'T' separator "
            "and optional trailing 'Z' (e.g. '2026-04-15', '2026-04-15 12:00:00', '2026-04-15T12:00:00Z'). "
            "Omit as_of (None) for live view (default: exclude deprecated nodes/invalidated edges)."
        )
    if len(as_of) > 32:
        raise ValueError(
            f"as_of too long ({len(as_of)} > 32 chars): {as_of!r}. "
            "Fix: use a standard ISO 8601 string like '2026-04-15T12:00:00Z'."
        )
    if not ISO_TIMESTAMP_PATTERN.match(as_of):
        raise ValueError(
            f"Invalid as_of format: {as_of!r}. "
            f"Expected pattern: {ISO_TIMESTAMP_PATTERN.pattern}. "
            "Accepted: 'YYYY-MM-DD', 'YYYY-MM-DD HH:MM:SS', 'YYYY-MM-DDTHH:MM:SS', optionally trailing 'Z' and .fractional seconds. "
            "Fix: examples → '2026-04-15', '2026-04-15 12:00:00', '2026-04-15T12:00:00.123Z'."
        )
    return as_of


async def node_exists(db: aiosqlite.Connection, node_id: str) -> bool:
    cur = await db.execute(
        "SELECT 1 FROM graph_nodes WHERE id = ? LIMIT 1", (node_id,)
    )
    row = await cur.fetchone()
    return row is not None


def _row_to_neighbor(row: aiosqlite.Row, direction: str, edge_relation: str, source_file: str | None, source_line: int | None) -> dict[str, Any]:
    metadata_raw = row["metadata"] if "metadata" in row.keys() else None
    try:
        metadata = json.loads(metadata_raw) if metadata_raw else {}
    except (TypeError, ValueError):
        metadata = {}
    return {
        "id": row["id"],
        "type": row["type"],
        "name": row["name"],
        "qualified_name": row["qualified_name"],
        "file_path": row["file_path"],
        "line_number": row["line_number"],
        "metadata": metadata,
        "edge": {
            "relation": edge_relation,
            "direction": direction,
            "source_file": source_file,
            "source_line": source_line,
        },
    }


def _temporal_where_and_params(as_of: str | None) -> tuple[str, list[Any]]:
    """Build the temporal WHERE fragment for the neighbour join.

    Returns `(sql_fragment, extra_params)`. The fragment is empty string if
    the caller does not want temporal filtering — but the default behaviour
    (as_of=None) DOES filter out deprecated rows, matching the "exclude stale"
    contract documented at module level.

    The fragment applies to both the neighbour node `n` and the edge `e`:
      - n.deprecated_at IS NULL / n created before as_of and not yet deprecated
      - e.valid_until IS NULL / e valid at as_of
    """
    if as_of is None:
        # Default: live view. Exclude deprecated nodes + invalidated edges.
        return (
            " AND n.deprecated_at IS NULL AND e.valid_until IS NULL",
            [],
        )
    # Time-travel view.
    # - Node must exist at as_of: created_at <= as_of AND (deprecated_at IS NULL OR deprecated_at > as_of)
    # - Edge must be valid at as_of: first_seen_at <= as_of AND (valid_until IS NULL OR valid_until > as_of)
    # `first_seen_at` was backfilled to `created_at` in migration 067 so legacy
    # edges still have a value to compare against.
    return (
        " AND n.created_at <= ? "
        "AND (n.deprecated_at IS NULL OR n.deprecated_at > ?) "
        "AND e.first_seen_at <= ? "
        "AND (e.valid_until IS NULL OR e.valid_until > ?)",
        [as_of, as_of, as_of, as_of],
    )


async def get_neighbors(
    db: aiosqlite.Connection,
    node_id: str,
    relation: str | None = None,
    direction: Direction = "both",
    limit: int = 50,
    as_of: str | None = None,
    project: str | None = None,
    edge_types: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Return graph neighbors for `node_id`.

    direction:
      - "incoming": nodes that have an edge → node_id (callers, importers)
      - "outgoing": nodes that node_id points to (callees, imports)
      - "both": union of the two

    as_of (Fase 1d):
      - None (default): exclude rows with `deprecated_at` / `valid_until` set
      - "<iso-ts>": return the historical state (time-travel query)

    Fase 2:
    - `project` (str|None): filter SOURCE node by project_id (ARCH-01
      project_scope=source). Neighbours themselves may cross projects — this
      is intentional for multi-hop discovery.
    - `edge_types` (list[str]|None): widen beyond the default Fase-1 set
      {calls|imports|defines} to include cross-project relations
      (depends_on|mentions|refers_to|shares_tag|similar_to). Mutually exclusive
      with `relation` (use one or the other).

    For symmetric edges (shares_tag, similar_to) call `undirected_neighbors`
    directly: this function resolves them via the requested direction only.

    Returns at most `limit` rows, with limit clamped to [1, 200].
    """
    validate_node_id(node_id)
    validate_as_of(as_of)
    validate_project(project)
    validate_edge_types(edge_types)
    limit = max(1, min(int(limit), MAX_NEIGHBORS))

    where_relation = ""
    relation_params: list[Any] = []
    # `relation` (single) takes precedence for backward compat; `edge_types`
    # is the Fase-2 union filter. If both are passed, relation is the
    # intersection (a Fase-1 caller with a typo).
    if edge_types is not None:
        placeholders = ",".join("?" * len(edge_types))
        where_relation = f" AND e.relation IN ({placeholders})"
        relation_params.extend(edge_types)
    elif relation is not None:
        # Permit any legal edge type, not just the Fase-1 set, so callers who
        # know they want depends_on/cites/etc can pass relation= too.
        if relation not in EDGE_TYPES:
            raise ValueError(
                f"Invalid relation: {relation!r}. "
                f"Allowed values: {list(EDGE_TYPES)}. "
                "Fix: pass one of the listed relations, or set relation=None + edge_types=[...] for multi-type queries."
            )
        where_relation = " AND e.relation = ?"
        relation_params.append(relation)

    # Fase 2: source-project filter (ARCH-01). Uses the covering index
    # idx_graph_edges_project_relation when combined with a relation filter.
    where_project = ""
    project_params: list[Any] = []
    if project is not None:
        # project_scope=source: filter the source node, not the edge row.
        # For incoming direction the "source" in our SQL is `n.project_id`
        # (the caller side); for outgoing, `n.project_id` is the callee side.
        # We want the target (this node)'s project unchanged — the call site
        # below applies the filter on the neighbour row `n` to get the
        # "neighbours that live in project=X" semantics callers expect.
        where_project = " AND n.project_id = ?"
        project_params.append(project)

    temporal_sql, temporal_params = _temporal_where_and_params(as_of)

    db.row_factory = aiosqlite.Row

    results: list[dict[str, Any]] = []

    if direction in ("incoming", "both"):
        # nodes pointing TO node_id
        sql = (
            "SELECT n.id, n.type, n.name, n.qualified_name, n.file_path, "
            "n.line_number, n.metadata, e.relation AS edge_relation, "
            "e.source_file AS edge_source_file, e.source_line AS edge_source_line "
            "FROM graph_edges e JOIN graph_nodes n ON n.id = e.source_id "
            "WHERE e.target_id = ?" + where_relation + where_project + temporal_sql + " "
            "ORDER BY n.qualified_name LIMIT ?"
        )
        p: list[Any] = [node_id]
        p.extend(relation_params)
        p.extend(project_params)
        p.extend(temporal_params)
        p.append(limit)
        cur = await db.execute(sql, p)
        rows = await cur.fetchall()
        for r in rows:
            results.append(_row_to_neighbor(
                r,
                direction="incoming",
                edge_relation=r["edge_relation"],
                source_file=r["edge_source_file"],
                source_line=r["edge_source_line"],
            ))

    if direction in ("outgoing", "both"):
        sql = (
            "SELECT n.id, n.type, n.name, n.qualified_name, n.file_path, "
            "n.line_number, n.metadata, e.relation AS edge_relation, "
            "e.source_file AS edge_source_file, e.source_line AS edge_source_line "
            "FROM graph_edges e JOIN graph_nodes n ON n.id = e.target_id "
            "WHERE e.source_id = ?" + where_relation + where_project + temporal_sql + " "
            "ORDER BY n.qualified_name LIMIT ?"
        )
        p = [node_id]
        p.extend(relation_params)
        p.extend(project_params)
        p.extend(temporal_params)
        p.append(limit)
        cur = await db.execute(sql, p)
        rows = await cur.fetchall()
        for r in rows:
            results.append(_row_to_neighbor(
                r,
                direction="outgoing",
                edge_relation=r["edge_relation"],
                source_file=r["edge_source_file"],
                source_line=r["edge_source_line"],
            ))

    # When both directions are requested we may exceed `limit` (limit is per
    # direction). Trim to keep the contract: never return more than `limit`.
    return results[:limit]


async def get_neighbors_with_metadata(
    db: aiosqlite.Connection,
    node_id: str,
    relation: str | None = None,
    direction: Direction = "both",
    limit: int = 50,
    as_of: str | None = None,
    project: str | None = None,
    edge_types: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Get neighbours and freeze their metadata at request start (Fase 1b).

    The existing `get_neighbors` already projects `metadata` into each row,
    so this function is a thin semantic alias. Having a dedicated name lets
    callers (the ranker) document that they require the metadata snapshot,
    and guards against a future refactor dropping that field.

    Race-safety note (data-integrity H3): the ranker consumes the list
    returned here as its snapshot — it must not re-query `graph_nodes` mid
    scoring pass.

    `as_of` (Fase 1d) is passed through to `get_neighbors` for time-travel.
    Fase 2: `project` and `edge_types` threaded through.
    """
    return await get_neighbors(
        db=db,
        node_id=node_id,
        relation=relation,
        direction=direction,
        limit=limit,
        as_of=as_of,
        project=project,
        edge_types=edge_types,
    )


async def undirected_neighbors(
    db: aiosqlite.Connection,
    node_id: str,
    relation: str,
    limit: int = 50,
    as_of: str | None = None,
    project: str | None = None,
) -> list[dict[str, Any]]:
    """Fase 2 (ARCH-02): symmetric-edge helper.

    For relations marked symmetric (`shares_tag`, `similar_to`) the populator
    may have written the edge with either (A,B) or (B,A) ordering because the
    relationship itself is undirected. Callers asking "who shares a tag with
    me" must check both sides to avoid silent half-answers.

    This helper queries `source_id=? OR target_id=?` for the given node and
    returns the OTHER endpoint — never the node itself. Unlike `get_neighbors`
    the `direction` concept does not apply here (meaningless for symmetric
    edges), and `relation` is required (the reason you'd call this function).

    We deliberately do NOT dispatch from `get_neighbors` to this helper
    automatically: a caller asking for `direction='outgoing'` on shares_tag
    is explicit about wanting one-sided semantics (e.g. verifying the storage
    ordering in a test). The routers dispatch explicitly based on the
    SYMMETRIC_EDGES set.
    """
    validate_node_id(node_id)
    validate_as_of(as_of)
    validate_project(project)
    if relation not in EDGE_TYPES:
        raise ValueError(
            f"Invalid relation: {relation!r}. "
            f"Allowed values: {list(EDGE_TYPES)}. "
            f"Note: undirected_neighbors is meant for symmetric relations {sorted(SYMMETRIC_EDGES)} — "
            "for directed edges use get_neighbors() with direction='incoming'|'outgoing'|'both'."
        )
    limit = max(1, min(int(limit), MAX_NEIGHBORS))

    temporal_sql, temporal_params = _temporal_where_and_params(as_of)

    where_project = ""
    project_params: list[Any] = []
    if project is not None:
        where_project = " AND n.project_id = ?"
        project_params.append(project)

    db.row_factory = aiosqlite.Row

    # The OTHER endpoint: if node_id is source, return the target; else source.
    sql = (
        "SELECT n.id, n.type, n.name, n.qualified_name, n.file_path, "
        "n.line_number, n.metadata, e.relation AS edge_relation, "
        "e.source_file AS edge_source_file, e.source_line AS edge_source_line, "
        "CASE WHEN e.source_id = ? THEN 'outgoing' ELSE 'incoming' END AS edge_direction "
        "FROM graph_edges e "
        "JOIN graph_nodes n ON n.id = CASE "
        "    WHEN e.source_id = ? THEN e.target_id "
        "    ELSE e.source_id "
        "END "
        "WHERE (e.source_id = ? OR e.target_id = ?) "
        "AND e.relation = ? "
        "AND n.id != ? "
        + where_project + temporal_sql
        + " ORDER BY n.qualified_name LIMIT ?"
    )
    params: list[Any] = [node_id, node_id, node_id, node_id, relation, node_id]
    params.extend(project_params)
    params.extend(temporal_params)
    params.append(limit)
    cur = await db.execute(sql, params)
    rows = await cur.fetchall()

    results: list[dict[str, Any]] = []
    for r in rows:
        results.append(_row_to_neighbor(
            r,
            direction=r["edge_direction"],
            edge_relation=r["edge_relation"],
            source_file=r["edge_source_file"],
            source_line=r["edge_source_line"],
        ))
    return results


# ---------------------------------------------------------------------------
# Hotspots (Fase 1e)
# ---------------------------------------------------------------------------


HotspotWindow = Literal["7d", "30d", "total"]
HotspotTypeFilter = Literal["function", "file", "all"]

# Allowed window tokens → concrete column name. Keeping the mapping explicit
# prevents ORM-style column injection via the `window` query param: even though
# FastAPI's Literal typing already rejects unknown values, a whitelist here is
# cheap defense in depth against a refactor that widens the accepted set.
_HOTSPOT_COLUMN = {
    "7d": "touch_count_7d",
    "30d": "touch_count_30d",
    "total": "touch_count_total",
}

MAX_HOTSPOT_LIMIT = 100


async def get_hotspots(
    db: aiosqlite.Connection,
    window: HotspotWindow = "30d",
    limit: int = 20,
    type_filter: HotspotTypeFilter = "file",
    project: str | None = None,
) -> list[dict[str, Any]]:
    """Return the top N nodes by touch count in the requested window.

    `window`:
      - "7d" / "30d": rolling churn windows populated by
        `core.scripts.populate_touch_counter` (commits touching the file within the
        last N days)
      - "total": all-time touch count

    `type_filter`:
      - "function": only function nodes (fine-grained — file touch is
        propagated to every function in that file, so callers get file-level
        signal on function ids)
      - "file": only file nodes (default — matches the mental model of
        "which module is hottest")
      - "all": both function AND file nodes (intentionally excludes
        `module` because modules are usually a 1:1 restate of files and
        would double-count in the ranking)

    `limit`: clamped to [1, 100] regardless of caller.

    Deprecated nodes (Fase 1d) are excluded — a file that was flagged stale
    can't be a current hotspot.

    Returns a list of dicts sorted DESC by the selected window's column, each
    with:
      id, type, name, qualified_name, file_path,
      touch_count_total, touch_count_7d, touch_count_30d,
      touch_authors (list[str]), touch_last_at
    """
    if window not in _HOTSPOT_COLUMN:
        raise ValueError(
            f"Invalid window: {window!r}. "
            f"Allowed values: {sorted(_HOTSPOT_COLUMN.keys())}. "
            "Fix: pick a supported aggregation window (e.g. 'all', '7d', '30d')."
        )
    col = _HOTSPOT_COLUMN[window]

    if type_filter == "all":
        type_clause = "AND type IN ('function','file')"
    elif type_filter in ("function", "file"):
        type_clause = f"AND type = '{type_filter}'"
    else:
        raise ValueError(
            f"Invalid type_filter: {type_filter!r}. "
            "Allowed values: 'all' (function+file), 'function', 'file'. "
            "Fix: pass one of the three options; other node types (module, artifact) are not hotspot-tracked."
        )

    validate_project(project)
    project_clause = ""
    project_params: list[Any] = []
    if project is not None:
        project_clause = "AND project_id = ? "
        project_params.append(project)

    limit = max(1, min(int(limit), MAX_HOTSPOT_LIMIT))

    db.row_factory = aiosqlite.Row

    sql = (
        "SELECT id, type, name, qualified_name, file_path, "
        "touch_count_total, touch_count_7d, touch_count_30d, "
        "touch_authors, touch_last_at "
        "FROM graph_nodes "
        "WHERE deprecated_at IS NULL "
        f"{type_clause} "
        f"{project_clause}"
        f"ORDER BY {col} DESC, touch_last_at DESC "
        "LIMIT ?"
    )
    cur = await db.execute(sql, (*project_params, limit))
    rows = await cur.fetchall()

    results: list[dict[str, Any]] = []
    for r in rows:
        try:
            authors = json.loads(r["touch_authors"]) if r["touch_authors"] else []
        except (TypeError, ValueError):
            authors = []
        results.append(
            {
                "id": r["id"],
                "type": r["type"],
                "name": r["name"],
                "qualified_name": r["qualified_name"],
                "file_path": r["file_path"],
                "touch_count_total": r["touch_count_total"],
                "touch_count_7d": r["touch_count_7d"],
                "touch_count_30d": r["touch_count_30d"],
                "touch_authors": authors,
                "touch_last_at": r["touch_last_at"],
            }
        )
    return results


async def find_recently_touched_doc_nodes(
    *,
    since_iso: str,
    kinds: tuple[str, ...],
    limit: int = 200,
) -> list[dict[str, Any]]:
    """Return doc-kind nodes whose `touch_last_at` advanced past `since_iso`.

    Centralised here so Brain's git_kg source collector stays free of direct
    SELECT FROM graph_nodes (anti-pattern: §7.1 plan; enforced via grep).
    Read-only — uses the pool acquire helper from api.db.
    """
    from core.api.db import acquire_db

    if not kinds:
        return []
    placeholders = ",".join("?" for _ in kinds)
    sql = (
        "SELECT id, type, name, qualified_name, file_path, project_id, "
        "       last_modified_git_sha, first_seen_git_sha, "
        "       touch_count_30d, touch_last_at "
        "FROM graph_nodes "
        f"WHERE type IN ({placeholders}) "
        "  AND touch_last_at IS NOT NULL "
        "  AND touch_last_at > ? "
        "  AND deprecated_at IS NULL "
        "ORDER BY touch_last_at DESC "
        "LIMIT ?"
    )
    capped = max(1, min(int(limit), 500))
    params: tuple[Any, ...] = (*kinds, since_iso, capped)

    async with acquire_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(sql, params)
        rows = await cur.fetchall()

    return [
        {
            "id": r["id"],
            "type": r["type"],
            "name": r["name"],
            "qualified_name": r["qualified_name"],
            "file_path": r["file_path"],
            "project_id": r["project_id"],
            "last_modified_git_sha": r["last_modified_git_sha"],
            "first_seen_git_sha": r["first_seen_git_sha"],
            "touch_count_30d": r["touch_count_30d"],
            "touch_last_at": r["touch_last_at"],
        }
        for r in rows
    ]


async def node_exists_at(
    db: aiosqlite.Connection, node_id: str, as_of: str | None = None
) -> bool:
    """Like `node_exists` but temporal-aware.

    If `as_of` is None, excludes deprecated nodes (matches default query
    behaviour). If `as_of` is an ISO timestamp, returns True iff the node
    existed at that moment.
    """
    if as_of is None:
        cur = await db.execute(
            "SELECT 1 FROM graph_nodes WHERE id = ? AND deprecated_at IS NULL LIMIT 1",
            (node_id,),
        )
    else:
        validate_as_of(as_of)
        cur = await db.execute(
            "SELECT 1 FROM graph_nodes WHERE id = ? "
            "AND created_at <= ? AND (deprecated_at IS NULL OR deprecated_at > ?) "
            "LIMIT 1",
            (node_id, as_of, as_of),
        )
    row = await cur.fetchone()
    return row is not None


# ---------------------------------------------------------------------------
# Fase 1f — graph_impact / graph_context / graph_pattern
# ---------------------------------------------------------------------------


MAX_IMPACT_DEPTH: int = 5
MAX_IMPACT_RESULTS: int = 200
DEFAULT_IMPACT_DEPTH: int = 2
DEFAULT_IMPACT_LIMIT: int = 50

# Context limits per category (context rot safety). Chain:
#   function --touches (inverse)--> commit
#   commit   --contains (inverse)--> pr
#   pr       --produces (inverse)--> task
#   task     --describes (inverse)--> handoff
#   handoff  --cites    (outgoing)--> learning
#   <any>    --applies_to (inverse)--> learning
DEFAULT_CONTEXT_PER_CATEGORY: int = 5
MAX_CONTEXT_PER_CATEGORY: int = 20

DEFAULT_PATTERN_LIMIT: int = 20
MAX_PATTERN_LIMIT: int = 100

# Scope-normalization hint used by graph_pattern.
MAX_SCOPE_LEN: int = 256


def _node_row_to_dict(row: aiosqlite.Row) -> dict[str, Any]:
    """Canonical projection for node rows used by 1f endpoints."""
    metadata_raw = row["metadata"] if "metadata" in row.keys() else None
    try:
        metadata = json.loads(metadata_raw) if metadata_raw else {}
    except (TypeError, ValueError):
        metadata = {}
    out: dict[str, Any] = {
        "id": row["id"],
        "type": row["type"],
        "name": row["name"],
        "qualified_name": row["qualified_name"],
        "file_path": row["file_path"] if "file_path" in row.keys() else None,
        "line_number": row["line_number"] if "line_number" in row.keys() else None,
        "metadata": metadata,
    }
    return out


async def graph_impact(
    db: aiosqlite.Connection,
    node_id: str,
    depth: int = DEFAULT_IMPACT_DEPTH,
    limit: int = DEFAULT_IMPACT_LIMIT,
    project: str | None = None,
    edge_types: list[str] | None = None,
) -> dict[str, Any]:
    """Reverse impact analysis for `node_id`.

    Answers "if I change this function, what breaks and which dependents are
    suspect?". Direct callers (depth=1) get the `suspect_write` ranker applied
    so the agent can filter noise. Transitive callers (depth>=2) are listed
    without score — context rot safety — up to a hard cap.

    Fase 2:
    - `project`: filter source nodes to the named project (ARCH-01)
    - `edge_types`: walk along a union of edge types instead of the
      Fase-1 default `calls`. Includes cross-project relations
      (`depends_on`, `mentions`, `refers_to`, `shares_tag`, `similar_to`).
      For symmetric edges in the set, the walk treats them as undirected
      (visits both endpoints).
    - BFS cycle protection: visited set (DI-A9) + hard cap
      `MAX_MULTI_HOP_NODES_VISITED=500` (PERF-7) + depth cap
      `MAX_MULTI_HOP_DEPTH=4` even when caller asks for more.

    Args:
      node_id: must match NODE_ID_PATTERN.
      depth: BFS hops, clamped to [1, min(MAX_IMPACT_DEPTH, MAX_MULTI_HOP_DEPTH)].
      limit: max transitive callers, clamped to [1, MAX_IMPACT_RESULTS].
      project: optional project_id filter on direct + transitive nodes.
      edge_types: optional list of edge types (union). None → legacy `calls`.

    Returns:
      {
        "target": node_id,
        "direct_callers": [{...node + score + classification + signals}],
        "transitive_callers": [{...node, distance}],
        "summary": {"suspect": N, "uncertain": N, "legitimate": N,
                    "direct": N, "transitive": N, "truncated": bool}
      }
    """
    # Local import to avoid circular (graph_ranker imports graph.py models).
    from core.api.services import graph_ranker

    validate_node_id(node_id)
    validate_project(project)
    validate_edge_types(edge_types)
    # Cap depth at both the Fase-1 limit and the Fase-2 multi-hop safety cap.
    effective_depth_cap = min(MAX_IMPACT_DEPTH, MAX_MULTI_HOP_DEPTH)
    depth = max(1, min(int(depth), effective_depth_cap))
    limit = max(1, min(int(limit), MAX_IMPACT_RESULTS))

    # Resolve edge-type set. Default behaviour preserves Fase-1 `calls`.
    walk_edges: list[str] = list(edge_types) if edge_types else ["calls"]
    symmetric_walk = any(r in SYMMETRIC_EDGES for r in walk_edges)

    db.row_factory = aiosqlite.Row

    # 1. Direct neighbours. For legacy calls-only case we keep the ranker path;
    # for multi-relation Fase-2 walks we also apply the ranker (it works fine
    # on any incoming set and correctly produces suspect/uncertain/legitimate).
    # When the walk includes symmetric edges we union both directions so
    # shares_tag/similar_to siblings show up at depth=1 too.
    if symmetric_walk:
        # Gather direct neighbours across any relation in walk_edges (both
        # sides for symmetric ones, incoming only for the rest to preserve
        # impact semantics).
        direct: list[dict[str, Any]] = []
        seen_direct: set[str] = {node_id}
        for rel in walk_edges:
            if rel in SYMMETRIC_EDGES:
                nbs = await undirected_neighbors(
                    db, node_id=node_id, relation=rel,
                    limit=MAX_IMPACT_RESULTS, project=project,
                )
            else:
                nbs = await get_neighbors(
                    db, node_id=node_id, relation=rel, direction="incoming",
                    limit=MAX_IMPACT_RESULTS, project=project,
                )
            for n in nbs:
                if n["id"] in seen_direct:
                    continue
                seen_direct.add(n["id"])
                direct.append(n)
    else:
        direct = await get_neighbors(
            db,
            node_id=node_id,
            direction="incoming",
            limit=MAX_IMPACT_RESULTS,
            project=project,
            edge_types=walk_edges if edge_types else None,
            relation="calls" if not edge_types else None,
        )

    ranked_direct = await graph_ranker.rank_suspect_write(db, direct)
    summary = {"suspect": 0, "uncertain": 0, "legitimate": 0}
    for r in ranked_direct:
        summary[r["classification"]] = summary.get(r["classification"], 0) + 1

    # 2. Transitive callers: BFS reverse walk, levels 2..depth.
    # Cycle protection (DI-A9/PERF-7):
    # - `visited` includes node_id + direct ids + every expanded node, so a
    #   cycle A→B→A→... can't loop.
    # - Hard cap `MAX_MULTI_HOP_NODES_VISITED` aborts the walk mid-hop if
    #   the graph is pathologically dense (defense in depth, not a real
    #   limit on MarvisX-scale graphs).
    direct_ids = {r["id"] for r in ranked_direct}
    transitive: list[dict[str, Any]] = []
    visited: set[str] = {node_id} | direct_ids
    frontier: set[str] = direct_ids.copy()
    truncated = False

    # Build the relation IN-clause once; reuse across hops.
    rel_placeholders = ",".join("?" * len(walk_edges))
    project_clause = ""
    project_bindings: list[Any] = []
    if project is not None:
        project_clause = " AND n.project_id = ?"
        project_bindings = [project]

    for hop in range(2, depth + 1):
        if not frontier or len(transitive) >= limit:
            break
        if len(visited) >= MAX_MULTI_HOP_NODES_VISITED:
            truncated = True
            break
        placeholders = ",".join("?" * len(frontier))
        if symmetric_walk:
            # For symmetric edges we accept either end of the edge matching
            # the frontier. SQL UNION is cleaner than a compound predicate
            # because SQLite query planner handles each branch independently.
            sql = (
                "SELECT n.id, n.type, n.name, n.qualified_name, n.file_path, "
                "n.line_number, n.metadata, e.source_id AS via "
                "FROM graph_edges e JOIN graph_nodes n ON n.id = e.source_id "
                f"WHERE e.target_id IN ({placeholders}) "
                f"AND e.relation IN ({rel_placeholders}) "
                f"AND n.deprecated_at IS NULL "
                "AND e.valid_until IS NULL "
                + project_clause + " "
                "UNION "
                "SELECT n.id, n.type, n.name, n.qualified_name, n.file_path, "
                "n.line_number, n.metadata, e.target_id AS via "
                "FROM graph_edges e JOIN graph_nodes n ON n.id = e.target_id "
                f"WHERE e.source_id IN ({placeholders}) "
                f"AND e.relation IN ({rel_placeholders}) "
                "AND n.deprecated_at IS NULL "
                "AND e.valid_until IS NULL "
                + project_clause + " "
                "ORDER BY qualified_name"
            )
            params: list[Any] = (
                list(frontier) + walk_edges + project_bindings
                + list(frontier) + walk_edges + project_bindings
            )
        else:
            sql = (
                "SELECT n.id, n.type, n.name, n.qualified_name, n.file_path, "
                "n.line_number, n.metadata, e.source_id AS via "
                "FROM graph_edges e JOIN graph_nodes n ON n.id = e.source_id "
                f"WHERE e.target_id IN ({placeholders}) "
                f"AND e.relation IN ({rel_placeholders}) "
                "AND n.deprecated_at IS NULL "
                "AND e.valid_until IS NULL "
                + project_clause + " "
                "ORDER BY n.qualified_name"
            )
            params = list(frontier) + walk_edges + project_bindings
        cur = await db.execute(sql, params)
        rows = await cur.fetchall()

        next_frontier: set[str] = set()
        for r in rows:
            nid = r["id"]
            if nid in visited:
                # DI-A9: cycle detection in action — a node already on the
                # walk is silently dropped (its contribution at this depth
                # doesn't add new information).
                continue
            visited.add(nid)
            if len(visited) > MAX_MULTI_HOP_NODES_VISITED:
                truncated = True
                break
            node_dict = _node_row_to_dict(r)
            node_dict["distance"] = hop
            transitive.append(node_dict)
            next_frontier.add(nid)
            if len(transitive) >= limit:
                break
        frontier = next_frontier

    summary["direct"] = len(ranked_direct)
    summary["transitive"] = len(transitive)
    summary["truncated"] = truncated

    return {
        "target": node_id,
        "direct_callers": ranked_direct,
        "transitive_callers": transitive,
        "summary": summary,
    }


async def graph_context(
    db: aiosqlite.Connection,
    node_id: str,
    per_category_limit: int = DEFAULT_CONTEXT_PER_CATEGORY,
    project: str | None = None,
) -> dict[str, Any]:
    """Rationale chain for `node_id`.

    Answers "why does this code exist?". Walks the Fase 1c/1e artefact graph
    starting from a code node (function/file/module):

      1. `learnings applies_to node` (or its module/file ancestors)
      2. `handoffs cites learnings` (outgoing from handoff)
      3. `tasks ← handoffs describe` (describes inverse)
      4. `PRs ← tasks produce`        (produces inverse)
      5. commits (best effort): nodes referencing the same file_path in
         `metadata.files`, or recent `commit:artifact:*` nodes whose commit
         touched the file according to populate_touch_counter —
         surfaced via `touch_last_at` when no edge exists.

    Design note: the plan (v2) called for `touches` edges, but Fase 1d
    implemented touch data as *counters on the node itself*, not as explicit
    `touches` edges — so the function→commit hop is surfaced via the
    `commit:artifact:*` candidate list filtered by `metadata.files`
    (populated by populate_commits). When no such metadata match exists the
    `commits` list is empty, and the rationale starts from learnings/handoffs.

    Each category is hard-capped at `per_category_limit` (default 5, max 20)
    to keep the bundle small enough for an agent context window.

    Args:
      node_id: must match NODE_ID_PATTERN.
      per_category_limit: clamped to [1, MAX_CONTEXT_PER_CATEGORY].

    Returns:
      {
        "node": {node fields},
        "commits":   [...],  # commit:artifact nodes whose metadata.files cites node.file_path
        "prs":       [...],  # pr:artifact nodes producing the tasks below
        "tasks":     [...],  # tasks described by the handoffs below
        "handoffs":  [...],  # handoffs citing the learnings below
        "learnings": [...],  # learnings applies_to node + its module/file
        "counts":    {...}
      }
    """
    validate_node_id(node_id)
    validate_project(project)
    per_category_limit = max(
        1, min(int(per_category_limit), MAX_CONTEXT_PER_CATEGORY)
    )

    db.row_factory = aiosqlite.Row

    # 0. The node itself (so callers can detect missing node → 404).
    # Fase 2: when `project` is provided, require the source node lives in
    # that project (ARCH-01 project_scope=source). The chain nodes (commits,
    # PRs, tasks, handoffs, learnings) can cross projects freely — that's
    # exactly why multi-hop is useful.
    if project is not None:
        cur = await db.execute(
            "SELECT id, type, name, qualified_name, file_path, line_number, metadata "
            "FROM graph_nodes WHERE id = ? AND deprecated_at IS NULL "
            "AND project_id = ?",
            (node_id, project),
        )
    else:
        cur = await db.execute(
            "SELECT id, type, name, qualified_name, file_path, line_number, metadata "
            "FROM graph_nodes WHERE id = ? AND deprecated_at IS NULL",
            (node_id,),
        )
    node_row = await cur.fetchone()
    if node_row is None:
        return {
            "node": None,
            "commits": [],
            "prs": [],
            "tasks": [],
            "handoffs": [],
            "learnings": [],
            "counts": {"commits": 0, "prs": 0, "tasks": 0, "handoffs": 0, "learnings": 0},
        }
    node_dict = _node_row_to_dict(node_row)

    # Build the set of scope ids that learnings might apply_to:
    # the node itself + its parent module (if function) + its file (best effort).
    scope_ids: set[str] = {node_id}
    try:
        lang, kind, qn = node_id.split(":", 2)
        if lang in {"py", "ts"}:
            if kind == "function":
                parent = qn.rsplit(".", 1)[0] if "." in qn else qn
                scope_ids.add(f"{lang}:module:{parent}")
                scope_ids.add(f"{lang}:file:{parent}")
            elif kind == "file":
                scope_ids.add(f"{lang}:module:{qn}")
            elif kind == "module":
                scope_ids.add(f"{lang}:file:{qn}")
    except ValueError:
        pass

    async def _neighbours_by_relation_inverse(
        target_ids: list[str] | set[str], relation: str, lim: int
    ) -> list[dict[str, Any]]:
        """Return nodes that point TO any of `target_ids` via `relation`."""
        if not target_ids:
            return []
        ids = list(target_ids)
        placeholders = ",".join("?" * len(ids))
        sql = (
            "SELECT DISTINCT n.id, n.type, n.name, n.qualified_name, "
            "n.file_path, n.line_number, n.metadata "
            "FROM graph_edges e JOIN graph_nodes n ON n.id = e.source_id "
            f"WHERE e.target_id IN ({placeholders}) "
            "AND e.relation = ? "
            "AND n.deprecated_at IS NULL "
            "AND e.valid_until IS NULL "
            "ORDER BY n.qualified_name "
            "LIMIT ?"
        )
        cur = await db.execute(sql, [*ids, relation, lim])
        rows = await cur.fetchall()
        return [_node_row_to_dict(r) for r in rows]

    async def _neighbours_by_relation_outgoing(
        source_ids: list[str] | set[str], relation: str, lim: int
    ) -> list[dict[str, Any]]:
        """Return nodes that `source_ids` point TO via `relation`."""
        if not source_ids:
            return []
        ids = list(source_ids)
        placeholders = ",".join("?" * len(ids))
        sql = (
            "SELECT DISTINCT n.id, n.type, n.name, n.qualified_name, "
            "n.file_path, n.line_number, n.metadata "
            "FROM graph_edges e JOIN graph_nodes n ON n.id = e.target_id "
            f"WHERE e.source_id IN ({placeholders}) "
            "AND e.relation = ? "
            "AND n.deprecated_at IS NULL "
            "AND e.valid_until IS NULL "
            "ORDER BY n.qualified_name "
            "LIMIT ?"
        )
        cur = await db.execute(sql, [*ids, relation, lim])
        rows = await cur.fetchall()
        return [_node_row_to_dict(r) for r in rows]

    # 1. Learnings directly applied to the node/module/file (applies_to inverse).
    applied = await _neighbours_by_relation_inverse(
        scope_ids, "applies_to", per_category_limit
    )
    applied_ids = {learning["id"] for learning in applied}

    # 2. Handoffs that cite those learnings.
    handoffs_from_cites: list[dict[str, Any]] = []
    if applied_ids:
        # Walk cites edges inverse: handoff --cites--> learning.
        placeholders = ",".join("?" * len(applied_ids))
        sql = (
            "SELECT DISTINCT n.id, n.type, n.name, n.qualified_name, "
            "n.file_path, n.line_number, n.metadata "
            "FROM graph_edges e JOIN graph_nodes n ON n.id = e.source_id "
            f"WHERE e.target_id IN ({placeholders}) "
            "AND e.relation = 'cites' "
            "AND n.type = 'handoff' "
            "AND n.deprecated_at IS NULL "
            "AND e.valid_until IS NULL "
            "ORDER BY n.qualified_name "
            "LIMIT ?"
        )
        cur = await db.execute(
            sql, [*applied_ids, per_category_limit]
        )
        rows = await cur.fetchall()
        handoffs_from_cites = [_node_row_to_dict(r) for r in rows]

    handoff_ids = {h["id"] for h in handoffs_from_cites}

    # 3. Tasks described by those handoffs (describes outgoing from handoff).
    tasks = await _neighbours_by_relation_outgoing(
        handoff_ids, "describes", per_category_limit
    )
    task_ids = {t["id"] for t in tasks}

    # 4. PRs produced by those tasks (produces outgoing from task).
    prs = await _neighbours_by_relation_outgoing(
        task_ids, "produces", per_category_limit
    )

    # 5. Commits — best effort via metadata.files match on commit nodes.
    # commit:artifact:* nodes don't currently carry a touches edge (Fase 1d
    # kept touches as node-level counters), so we fall back to a JSON LIKE
    # scan over commit metadata. We keep the scan bounded by the hard cap.
    commits: list[dict[str, Any]] = []
    file_path = node_dict.get("file_path")
    if file_path:
        # Safe pattern: we only match by substring of a path. SQL `LIKE` with
        # parameters is injection-safe; we wrap the path in JSON-quote context
        # so `"files": [... "file_path" ...]` matches. Escape `%` and `_` in
        # user input — but file_path is from the graph itself (not user input)
        # so escape is defensive only.
        like_pattern = f'%"{file_path}"%'
        cur = await db.execute(
            "SELECT id, type, name, qualified_name, file_path, line_number, metadata "
            "FROM graph_nodes "
            "WHERE type = 'commit' "
            "AND metadata LIKE ? "
            "AND deprecated_at IS NULL "
            "ORDER BY created_at DESC "
            "LIMIT ?",
            (like_pattern, per_category_limit),
        )
        rows = await cur.fetchall()
        commits = [_node_row_to_dict(r) for r in rows]

    return {
        "node": node_dict,
        "commits": commits,
        "prs": prs,
        "tasks": tasks,
        "handoffs": handoffs_from_cites,
        "learnings": applied,
        "counts": {
            "commits": len(commits),
            "prs": len(prs),
            "tasks": len(tasks),
            "handoffs": len(handoffs_from_cites),
            "learnings": len(applied),
        },
    }


def _scope_to_candidate_node_ids(scope: str) -> list[str]:
    """Normalize a free-form scope string to candidate KG node ids.

    Accepted forms:
      - file path: "api/db.py", "console/src/components/Foo.tsx" → file node
        + derived module node (dotted, stripped extension).
      - module qualified name: "api.db", "console.src.components.Foo" → module
        node only (we can't uniquely map a module back to a file without the
        language prefix).
      - full node id: "py:function:api.db.get_db" → returned as-is (caller is
        being explicit).

    We produce a short candidate list (never more than ~4 entries) so the
    caller can OR them in a single SQL.
    """
    s = scope.strip()
    candidates: list[str] = []

    # Already a full node id.
    if NODE_ID_PATTERN.match(s):
        candidates.append(s)
        # Also include the parent module when the id is a function/file.
        try:
            lang, kind, qn = s.split(":", 2)
        except ValueError:
            return candidates
        if lang in {"py", "ts"} and kind in {"function", "file"}:
            # Parent module for a function is everything before the last dot.
            if kind == "function":
                parent = qn.rsplit(".", 1)[0] if "." in qn else qn
                candidates.append(f"{lang}:module:{parent}")
            if kind == "file":
                # File ids encode dotted paths already, so the module id is a
                # direct prefix swap.
                candidates.append(f"{lang}:module:{qn}")
        return candidates

    # Heuristic: file-shaped input (starts with `/`, contains `/`, or ends
    # with a known source extension) → map to module dotted form.
    is_file_shaped = (
        s.startswith("/")
        or "/" in s
        or s.endswith(".py")
        or s.endswith(".ts")
        or s.endswith(".tsx")
        or s.endswith(".js")
        or s.endswith(".mjs")
    )
    if is_file_shaped:
        # Strip leading slash, strip extension, replace `/` with `.`.
        trimmed = s.lstrip("/")
        for ext in (".py", ".tsx", ".ts", ".mjs", ".js"):
            if trimmed.endswith(ext):
                trimmed = trimmed[: -len(ext)]
                # Extension implies language.
                lang = "py" if ext == ".py" else "ts"
                break
        else:
            # Unknown extension — assume py by default (codebase majority).
            lang = "py"
        dotted = trimmed.replace("/", ".")
        # Safe slug (drop anything outside the allowed regex charset).
        dotted = re.sub(r"[^a-zA-Z0-9_\-.]", "_", dotted)
        if dotted:
            candidates.append(f"{lang}:module:{dotted}")
            candidates.append(f"{lang}:file:{dotted}")
        return candidates

    # Module dotted form: try both py and ts.
    safe = re.sub(r"[^a-zA-Z0-9_\-.]", "_", s)
    if safe:
        candidates.append(f"py:module:{safe}")
        candidates.append(f"ts:module:{safe}")
    return candidates


async def graph_pattern(
    db: aiosqlite.Connection,
    scope: str,
    limit: int = DEFAULT_PATTERN_LIMIT,
    project: str | None = None,
) -> dict[str, Any]:
    """Learnings applicable to `scope` (file path, module, or function).

    Answers "what patterns should I follow when writing code here?". Returns
    learnings with `applies_to` edges pointing at any of:
      - the literal node id if `scope` is a full id (py:function:..., etc.)
      - the derived module node id if `scope` is a file path
      - any descendant function of the module (for file/module scopes) — the
        learning may apply to a specific helper rather than the whole module

    Args:
      scope: free-form string; see `_scope_to_candidate_node_ids` for parse.
      limit: clamped to [1, MAX_PATTERN_LIMIT].

    Returns:
      {
        "scope": scope,
        "resolved_nodes": [node_id candidates considered],
        "learnings": [{id, title, severity, description, category, module, tags}],
        "total": N
      }
    """
    if not isinstance(scope, str) or not scope:
        raise ValueError(
            "scope is required. "
            "Expected: a file path, function qualified_name, or node_id "
            "(e.g. 'api/db.py', 'api.db.get_db', 'py:function:api.db.get_db'). "
            "The function resolves scope to candidate node_ids via _scope_to_candidate_node_ids, "
            "then looks up learnings attached to any of them."
        )
    if len(scope) > MAX_SCOPE_LEN:
        raise ValueError(
            f"scope too long ({len(scope)} > {MAX_SCOPE_LEN}): {scope[:80]!r}... "
            "Fix: pass a single path/qualified_name/node_id, not concatenated scopes."
        )
    validate_project(project)
    limit = max(1, min(int(limit), MAX_PATTERN_LIMIT))

    candidates = _scope_to_candidate_node_ids(scope)
    if not candidates:
        return {"scope": scope, "resolved_nodes": [], "learnings": [], "total": 0}

    db.row_factory = aiosqlite.Row

    # Build dynamic IN clause over the candidate set. Also expand the module
    # candidates to include their descendant function/file nodes in scope —
    # a learning `applies_to py:function:api.db.get_db` must surface when the
    # user asks for scope="api/db.py" (the function is under that file/module).
    module_candidates = [c for c in candidates if ":module:" in c]
    descendants: set[str] = set()
    if module_candidates:
        # For each `lang:module:prefix`, find every function or file node whose
        # qualified_name starts with `prefix.` (descendant) or equals prefix.
        for cand in module_candidates:
            try:
                lang, _, qn = cand.split(":", 2)
            except ValueError:
                continue
            like = qn + ".%"
            cur = await db.execute(
                "SELECT id FROM graph_nodes "
                "WHERE (type = 'function' OR type = 'file') "
                "AND (qualified_name = ? OR qualified_name LIKE ?) "
                "AND id LIKE ? || ':%' "
                "AND deprecated_at IS NULL "
                "LIMIT 500",
                (qn, like, lang),
            )
            rows = await cur.fetchall()
            for r in rows:
                descendants.add(r["id"])

    target_set = set(candidates) | descendants
    if not target_set:
        return {
            "scope": scope,
            "resolved_nodes": candidates,
            "learnings": [],
            "total": 0,
        }

    ids_list = list(target_set)
    placeholders = ",".join("?" * len(ids_list))
    project_clause = ""
    project_params: list[Any] = []
    if project is not None:
        # Fase 2: filter by project of the `applies_to` target node (the code
        # node that has learnings attached), not the learning itself. This is
        # the natural semantics of "learnings applicable to code in project X".
        project_clause = " AND EXISTS (SELECT 1 FROM graph_nodes tn WHERE tn.id = e.target_id AND tn.project_id = ?)"
        project_params.append(project)
    sql = (
        "SELECT DISTINCT n.id, n.type, n.name, n.qualified_name, "
        "n.metadata, n.created_at "
        "FROM graph_edges e JOIN graph_nodes n ON n.id = e.source_id "
        f"WHERE e.target_id IN ({placeholders}) "
        "AND e.relation = 'applies_to' "
        "AND n.type = 'learning' "
        "AND n.deprecated_at IS NULL "
        "AND e.valid_until IS NULL "
        + project_clause + " "
        "ORDER BY n.created_at DESC "
        "LIMIT ?"
    )
    cur = await db.execute(sql, [*ids_list, *project_params, limit])
    rows = await cur.fetchall()

    learnings: list[dict[str, Any]] = []
    for r in rows:
        metadata_raw = r["metadata"]
        try:
            metadata = json.loads(metadata_raw) if metadata_raw else {}
        except (TypeError, ValueError):
            metadata = {}
        # Learning nodes carry their title inside `metadata.title` (the node
        # `name` column is the short id prefix, per Fase 1c populator). Fall
        # back to `name` if metadata is absent for any reason.
        learnings.append(
            {
                "id": r["id"],
                "title": metadata.get("title") or r["name"],
                "qualified_name": r["qualified_name"],
                "severity": metadata.get("severity"),
                "description": metadata.get("description"),
                "category": metadata.get("category"),
                "module": metadata.get("module"),
                "tags": metadata.get("tags", []),
            }
        )

    return {
        "scope": scope,
        "resolved_nodes": candidates,
        "learnings": learnings,
        "total": len(learnings),
    }


# ----------------------------------------------------------------------------
# Event-driven writers (KG bug fix 2026-04-17)
# ----------------------------------------------------------------------------
#
# Background: `scripts/populate_artifacts.py::populate_tasks_and_prs` runs as a
# batch job (cron / manual). Between batches, a newly-created task lives in the
# `tasks` table but NOT in `graph_nodes`, so `populate_handoffs` skips any
# handoff that references it with `orphan_reason=task_id_not_in_graph`. That
# silently starves the knowledge graph of recent context.
#
# Fix: emit the `task:artifact:{uuid}` node synchronously on `POST /tasks`
# (and on demand from elsewhere). The helper below mirrors the node shape
# produced by `populate_tasks_and_prs` so the batch job remains idempotent —
# re-running it over a task we already inserted is a no-op UPSERT refresh.
#
# Contract:
# - UPSERT (ON CONFLICT DO UPDATE) so duplicate calls are safe
# - Non-blocking: errors are logged and swallowed, never re-raised. The task
#   is already committed in the `tasks` table; a graph sync failure must not
#   surface to the client as a 500.
# - No edges written here. Edges (task → pr via `produces`, handoff → task via
#   `describes`) come from populators that have the full picture; doing them
#   here would require a PR or handoff we don't have yet.


def _sanitize_artifact_slug(raw: str) -> str:
    """Coerce a free-form string to a NODE_ID-safe slug (mirror of
    scripts.populate_artifacts._safe_slug, duplicated here to avoid pulling
    scripts/ as an import from the API service layer)."""
    slug = re.sub(r"[^a-zA-Z0-9_\-.]+", "_", str(raw).strip())
    slug = re.sub(r"_+", "_", slug).strip("_")
    return slug or "_unknown"


async def sync_task_to_graph(
    db: aiosqlite.Connection,
    *,
    task_id: str,
    title: str | None,
    project: str | None,
    priority: str | None,
    source: str | None,
    status: str,
    created_at: str,
    updated_at: str | None = None,
) -> bool:
    """Emit/refresh a `task:artifact:{uuid}` node in `graph_nodes`.

    Returns True on successful write, False on any failure (logged, never
    raised). Callers must treat the return value as informational — the
    task's authoritative home is the `tasks` table, this is a cache.

    Node shape mirrors `scripts/populate_artifacts.py::populate_tasks_and_prs`
    so the batch populator remains idempotent over rows this helper already
    inserted (same `id`, `type`, `qualified_name`, metadata schema).
    """
    import logging

    _logger = logging.getLogger(__name__)

    try:
        node_id = f"task:artifact:{_sanitize_artifact_slug(task_id)}"
        # Defense in depth: the constructed id must match the same regex the
        # read path validates. A bad task_id (e.g. bogus chars) means we just
        # skip the sync rather than poisoning the graph.
        if not NODE_ID_PATTERN.match(node_id) or len(node_id) > MAX_NODE_ID_LEN:
            _logger.warning(
                "sync_task_to_graph: refusing to write invalid node_id=%r (task_id=%r)",
                node_id,
                task_id,
            )
            return False

        metadata = {
            "uuid": task_id,
            "title": (title or "")[:300],
            "status": status,
            "priority": priority,
            "source": source,
            "created_at": created_at,
            "updated_at": updated_at or created_at,
            "project": project,
        }
        metadata_json = json.dumps(metadata, sort_keys=True, separators=(",", ":"))
        qualified_name = f"task.{task_id}"
        short_name = task_id[:8] if task_id else "task"
        project_id = project or "marvisx"

        # UPSERT — matches the shape emitted by scripts/_graph_writer.py so
        # re-runs of the batch populator stay idempotent (same columns, same
        # conflict target, `metadata` replaced wholesale).
        await db.execute(
            """
            INSERT INTO graph_nodes
                (id, type, name, qualified_name, file_path, line_number,
                 metadata, last_seen_at, project_id)
            VALUES (?, 'task', ?, ?, NULL, NULL, ?, datetime('now'), ?)
            ON CONFLICT(id) DO UPDATE SET
                type = excluded.type,
                name = excluded.name,
                qualified_name = excluded.qualified_name,
                metadata = excluded.metadata,
                last_seen_at = datetime('now'),
                updated_at = datetime('now'),
                project_id = COALESCE(graph_nodes.project_id, excluded.project_id)
            """,
            (node_id, short_name, qualified_name, metadata_json, project_id),
        )
        await db.commit()
        return True
    except Exception:
        # Non-blocking: the task is already persisted in the tasks table, a
        # graph sync failure must not surface as a 5xx to the API caller.
        _logger.warning(
            "sync_task_to_graph failed for task_id=%s (non-critical, swallowed)",
            task_id,
            exc_info=True,
        )
        return False
