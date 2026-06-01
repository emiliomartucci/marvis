# v1.0.0 - 2026-05-16 - KG PR-Impact sub-02 MVP: read-side service queries
"""Read-side queries for the PR-impact REST surface.

The producer side lives in `api/services/pr_impact_pipeline/` (sub-01).
This module owns the read path: take a `pr_id` (canonical
`pr:artifact:<uuid>`) and return the bundle of touched functions +
transitive impact + metadata that the Codex lens (sub-03) renders.

MVP defers:
- HMAC-signed cursor (offset pagination only for v1)
- Recursive CTE BFS deeper than 1 hop (single LEFT JOIN for v1)
- Cross-project visibility filter (left as TODO; for now we trust the
  caller's RBAC at the endpoint layer)
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Literal

import aiosqlite

from core.api.models.graph_pr_impact import (
    BranchItem,
    ConflictPair,
    ModifiedFunctionItem,
    PrMetadata,
    TransitiveImpactItem,
)

logger = logging.getLogger(__name__)


_FUNCTION_CAP_DEFAULT = 800


# --------------------------------------------------------------------------
# /pr-impact/{pr_id}
# --------------------------------------------------------------------------


async def get_pr_metadata(
    db: aiosqlite.Connection,
    pr_artifact_id: str,
) -> tuple[str | None, PrMetadata | None]:
    """Resolve the canonical pr_artifact_id -> (pull_requests.id, metadata).

    Returns (row_id, metadata). row_id is None when the PR doesn't exist;
    the endpoint layer should turn that into 404.
    """
    task_id = pr_artifact_id.removeprefix("pr:artifact:")
    async with db.execute(
        """
        SELECT pr.id, pr.task_id, pr.project, pr.branch, pr.target,
               pr.status, pr.title, pr.commit_sha, t.title
          FROM pull_requests pr
          LEFT JOIN tasks t ON t.id = pr.task_id
         WHERE pr.task_id = ? OR pr.id = ?
         ORDER BY pr.created_at DESC
         LIMIT 1
        """,
        (task_id, task_id),
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        return None, None

    (row_id, task_id_canonical, project, branch, target, status, title, head_sha, task_title) = row

    populator_status = await _summarize_populator_status(db, row_id)

    return row_id, PrMetadata(
        title=title or task_title,
        branch=branch,
        review_state=status,
        head_sha=head_sha if head_sha else None,
        base_sha=target or "main",
        populator_status=populator_status,
        function_nodes_returned=0,  # filled in by the caller
        function_cap_threshold=_FUNCTION_CAP_DEFAULT,
    )


async def _summarize_populator_status(
    db: aiosqlite.Connection, pr_row_id: str
) -> str:
    """Roll up pr_impact_jobs status to a single label for the metadata block."""
    async with db.execute(
        """
        SELECT status, COUNT(*) FROM pr_impact_jobs
         WHERE pr_id = ?
         GROUP BY status
        """,
        (pr_row_id,),
    ) as cur:
        rows = await cur.fetchall()
    if not rows:
        return "unknown"
    states = {r[0]: r[1] for r in rows}
    if states.get("done"):
        return "processed"
    if states.get("running") or states.get("queued"):
        return "pending"
    if states.get("dead"):
        return "failed"
    if states.get("failed"):
        return "failed"
    return "unknown"


async def list_modified_functions(
    db: aiosqlite.Connection,
    pr_row_id: str,
    *,
    offset: int = 0,
    limit: int = 50,
    cap: int = _FUNCTION_CAP_DEFAULT,
    include_all: bool = False,
) -> tuple[list[ModifiedFunctionItem], int]:
    """Return the touched functions for a PR, paginated.

    When the total count exceeds `cap` and `include_all=False`, we trim to
    the top `cap` rows ordered by weight DESC (a coarse priority proxy
    pending full ranking in v1.1).
    """
    effective_limit = limit
    if include_all:
        effective_limit = min(effective_limit, 10000)
    else:
        effective_limit = min(effective_limit, max(cap - offset, 0))

    if effective_limit <= 0:
        return [], 0

    async with db.execute(
        "SELECT COUNT(*) FROM pr_function_touches WHERE pr_id = ?",
        (pr_row_id,),
    ) as cur:
        (total,) = await cur.fetchone()

    async with db.execute(
        """
        SELECT
            ft.function_id,
            ft.qualified_name_snapshot,
            ft.source_file,
            ft.touched_lines,
            ft.diff_added,
            ft.diff_removed,
            ft.weight,
            ft.blame_author,
            ft.created_at,
            (ft.function_id IS NULL) AS node_missing
          FROM pr_function_touches ft
         WHERE ft.pr_id = ?
         ORDER BY ft.weight DESC, ft.diff_added DESC, ft.qualified_name_snapshot ASC
         LIMIT ? OFFSET ?
        """,
        (pr_row_id, effective_limit, offset),
    ) as cur:
        rows = await cur.fetchall()

    items = [
        ModifiedFunctionItem(
            node_id=r[0] or f"py:function:_orphan_{r[1]}",
            qualified_name_snapshot=r[1],
            source_file=r[2],
            touch_kind=_infer_touch_kind(r[4], r[5]),
            lines_added=r[4],
            lines_removed=r[5],
            weight=r[6],
            blame_author=r[7],
            node_missing=bool(r[9]),
        )
        for r in rows
    ]
    return items, total


def _infer_touch_kind(added: int, removed: int) -> str:
    """Backfill touch_kind from the diff numbers when the column isn't
    persisted (sub-01 D1 schema doesn't store it on the row, just in the
    metadata blob of the modifies edge)."""
    if removed > 0 and added == 0:
        return "delete"
    if added > 0 and removed == 0:
        return "add"
    return "modify"


async def list_transitive_impact(
    db: aiosqlite.Connection,
    pr_row_id: str,
    *,
    depth: int = 1,
    limit: int = 500,
) -> list[TransitiveImpactItem]:
    """Single-hop transitive impact via `calls`/`imports`/`defines` edges.

    MVP keeps this depth-1 to avoid the recursive CTE perf trap; the plan
    explicitly cites <150ms p95, and a single LEFT JOIN comfortably fits.
    Deeper BFS lands in v1.1 along with HMAC cursors.
    """
    if depth < 1:
        return []
    async with db.execute(
        """
        SELECT DISTINCT e.target_id, e.relation
          FROM pr_function_touches ft
          JOIN graph_edges e
            ON e.source_id = ft.function_id
           AND e.relation IN ('calls','imports','defines')
           AND (e.valid_until IS NULL OR e.valid_until > datetime('now'))
         WHERE ft.pr_id = ?
           AND ft.function_id IS NOT NULL
         LIMIT ?
        """,
        (pr_row_id, limit),
    ) as cur:
        rows = await cur.fetchall()
    return [
        TransitiveImpactItem(node_id=r[0], depth=1, via_edge=r[1])
        for r in rows
    ]


# --------------------------------------------------------------------------
# /branches
# --------------------------------------------------------------------------


async def list_branches(
    db: aiosqlite.Connection,
    *,
    state: str = "active",
    project: str | None = None,
    stale_days: int = 30,
    offset: int = 0,
    limit: int = 50,
) -> tuple[list[BranchItem], int]:
    """Live query over `pull_requests` grouped by branch.

    MVP doesn't read `commits` directly because the populator hasn't
    populated commit nodes for every branch yet. We derive freshness from
    `pull_requests.created_at` instead — good enough for the Codex lens
    sidebar; the canonical commit-based view lands in v1.1 once branches +
    commits tables ship.
    """
    conditions: list[str] = []
    params: list[Any] = []
    if project:
        conditions.append("pr.project = ?")
        params.append(project)

    state_filter = ""
    if state == "active":
        state_filter = "AND pr.status IN ('draft','open','merging')"
    elif state == "stale":
        state_filter = "AND pr.status IN ('open','draft')"
    where_sql = ("WHERE " + " AND ".join(conditions) + " " + state_filter) if conditions else (
        ("WHERE 1=1 " + state_filter) if state_filter else ""
    )

    total_sql = f"""
        SELECT COUNT(DISTINCT pr.branch)
          FROM pull_requests pr
          {where_sql}
    """
    async with db.execute(total_sql, params) as cur:
        (total,) = await cur.fetchone()

    list_sql = f"""
        SELECT pr.branch,
               MAX(pr.commit_sha) AS head_sha,
               MAX(pr.created_at) AS head_commit_at,
               (julianday('now') - julianday(MAX(pr.created_at))) AS age_days,
               GROUP_CONCAT(DISTINCT 'pr:artifact:' || pr.task_id) AS open_pr_artifacts
          FROM pull_requests pr
          {where_sql}
         GROUP BY pr.branch
         ORDER BY head_commit_at DESC
         LIMIT ? OFFSET ?
    """
    params_with_pagination = params + [limit, offset]
    async with db.execute(list_sql, params_with_pagination) as cur:
        rows = await cur.fetchall()

    items: list[BranchItem] = []
    for r in rows:
        branch, head_sha, head_commit_at, age_days, open_pr_str = r
        open_pr_ids = open_pr_str.split(",") if open_pr_str else []
        age_int = int(age_days) if age_days is not None else None
        items.append(
            BranchItem(
                name=branch,
                head_sha=head_sha if head_sha else None,
                head_commit_at=head_commit_at,
                is_main=branch in ("main", "master"),
                is_stale=bool(age_int and age_int > stale_days),
                open_pr_ids=open_pr_ids,
                age_days=age_int,
            )
        )
    return items, total


# --------------------------------------------------------------------------
# /conflicts
# --------------------------------------------------------------------------


async def find_conflicts(
    db: aiosqlite.Connection,
    pr_task_ids: list[str],
    *,
    project: str | None = None,
) -> list[ConflictPair]:
    """Find functions touched by >1 of the given PRs.

    Caller passes 2-5 PR task UUIDs; we resolve to pull_requests.id rows,
    intersect pr_function_touches by function_id, and emit a ConflictPair
    for each shared function with the participating PR ids.
    """
    if len(pr_task_ids) < 2:
        return []
    placeholders = ",".join("?" for _ in pr_task_ids)
    async with db.execute(
        f"""
        SELECT pr.id, pr.task_id
          FROM pull_requests pr
         WHERE pr.task_id IN ({placeholders}) OR pr.id IN ({placeholders})
        """,
        (*pr_task_ids, *pr_task_ids),
    ) as cur:
        pr_rows = await cur.fetchall()
    if len(pr_rows) < 2:
        return []
    row_id_to_task = {r[0]: r[1] for r in pr_rows}
    row_ids = list(row_id_to_task.keys())

    placeholders = ",".join("?" for _ in row_ids)
    sql = f"""
        SELECT
            ft.function_id,
            ft.qualified_name_snapshot,
            GROUP_CONCAT(DISTINCT 'pr:artifact:' || row_id_map.task_id) AS pr_artifacts,
            GROUP_CONCAT(DISTINCT CASE
                WHEN ft.diff_removed > 0 AND ft.diff_added = 0 THEN 'delete'
                WHEN ft.diff_added > 0 AND ft.diff_removed = 0 THEN 'add'
                ELSE 'modify' END) AS touch_kinds
          FROM pr_function_touches ft
          JOIN (
                SELECT id, task_id FROM pull_requests
                 WHERE id IN ({placeholders})
          ) row_id_map ON row_id_map.id = ft.pr_id
         WHERE ft.function_id IS NOT NULL
         GROUP BY ft.function_id, ft.qualified_name_snapshot
        HAVING COUNT(DISTINCT ft.pr_id) >= 2
    """
    async with db.execute(sql, row_ids) as cur:
        rows = await cur.fetchall()
    conflicts: list[ConflictPair] = []
    for r in rows:
        function_id, qname, artifacts_csv, kinds_csv = r
        pr_ids = artifacts_csv.split(",") if artifacts_csv else []
        kinds = kinds_csv.split(",") if kinds_csv else []
        if len(pr_ids) < 2:
            continue
        conflicts.append(
            ConflictPair(
                pr_ids=pr_ids,
                shared_function_id=function_id,
                shared_qualified_name=qname,
                touch_kinds=kinds,
            )
        )
    return conflicts


__all__ = [
    "get_pr_metadata",
    "list_modified_functions",
    "list_transitive_impact",
    "list_branches",
    "find_conflicts",
    "cluster_for_path",
    "list_codex_modules",
    "list_codex_module_edges",
    "list_codex_modules_with_edges",
    "list_codex_functions",
    "CodexModuleAggregate",
    "CodexModuleEdge",
    "CodexFunctionItem",
    "CodexClusterId",
]


# --------------------------------------------------------------------------
# /codex-modules + /codex-functions — semantic-cluster planet view
# --------------------------------------------------------------------------
#
# The Codex lens needs a "macro" view for the default state: planets are
# semantic modules (auth, db, api, ui, parse, search, graph, shared) and the
# user zooms in on a planet to see its functions. Brain v1 sub-03 will ship
# the ratified module names; until then we use a path-based heuristic that
# maps file paths to one of the 8 canonical clusters (matches
# `codex-page.jsx:CLUSTER_COLORS` so the colors stay aligned).


CodexClusterId = Literal[
    "auth", "db", "api", "ui", "parse", "search", "graph", "shared"
]


def cluster_for_path(path: str) -> CodexClusterId:
    """Map a graph_nodes file path / qualified_name to one of the 8 canonical
    Codex clusters. Pure function, no DB hit, so the SQL aggregation can
    inline it as a CASE statement when needed.
    """
    p = path.lower()
    if "auth" in p:
        return "auth"
    if any(seg in p for seg in ("db", "database", "sqlite", "migration", "schema")):
        return "db"
    if any(seg in p for seg in ("parse", "tree_sitter", "tree-sitter", "ast", "tokenizer")):
        return "parse"
    if any(seg in p for seg in ("search", "embedding", "voyage", "semantic")):
        return "search"
    if any(seg in p for seg in ("graph", "kg_", "/kg/", "knowledge", "cosmos", "cosmo")):
        return "graph"
    if any(seg in p for seg in ("console/", "components/", "ui/", "react", "tsx")):
        return "ui"
    if p.startswith("api/routers") or "/router" in p or "/handlers" in p:
        return "api"
    return "shared"


@dataclass(frozen=True)
class CodexModuleAggregate:
    slug: str
    cluster: CodexClusterId
    label: str
    function_count: int
    file_count: int
    degree: int
    top_functions: list[str]
    top_paths: list[str]
    semantic_label: str | None = None
    ratified: bool = False
    drift: int = 0


@dataclass(frozen=True)
class CodexModuleEdge:
    source: str
    target: str
    relation: str  # 'calls' | 'imports' | 'depends_on' | 'mentions'
    weight: int
    hot: bool = False


async def list_codex_modules(
    db: aiosqlite.Connection,
    *,
    project: str = "marvisx",
    limit: int = 24,
) -> list[CodexModuleAggregate]:
    """Aggregate function nodes by 2nd-level directory + cluster.

    Returns up to `limit` modules sorted by function_count DESC so the
    biggest semantic units land on the inner ring of the canvas.

    Bucketing uses `file_path` when available; falls back to deriving a
    pseudo-path from `qualified_name` (dots → slashes, drop last segment
    which is the function name) so the ~50% of function nodes without
    file_path still get attributed to the right module.
    """
    async with db.execute(
        """
        SELECT id, qualified_name, file_path, type
          FROM graph_nodes
         WHERE type IN ('function','file')
           AND deprecated_at IS NULL
           AND (project_id = ? OR project_id IS NULL)
         LIMIT 20000
        """,
        (project,),
    ) as cur:
        rows = await cur.fetchall()

    by_slug: dict[str, dict[str, Any]] = {}
    for _node_id, qname, file_path, node_type in rows:
        slug = _bucket_slug(file_path, qname, node_type)
        if not slug:
            continue
        bucket = by_slug.setdefault(
            slug,
            {
                "slug": slug,
                "cluster": cluster_for_path(slug),
                "function_count": 0,
                "file_count": 0,
                "top_functions": [],
                "top_paths": [],
            },
        )
        if node_type == "function":
            bucket["function_count"] += 1
            if len(bucket["top_functions"]) < 5 and qname:
                bucket["top_functions"].append(qname)
        else:
            bucket["file_count"] += 1
            if len(bucket["top_paths"]) < 5 and file_path:
                bucket["top_paths"].append(file_path)

    ranked = sorted(
        by_slug.values(),
        key=lambda b: (-b["function_count"], -b["file_count"], b["slug"]),
    )[:limit]
    return [
        CodexModuleAggregate(
            slug=b["slug"],
            cluster=b["cluster"],
            label=_module_label(b["slug"]),
            function_count=b["function_count"],
            file_count=b["file_count"],
            degree=b.get("degree", 0),
            top_functions=b["top_functions"],
            top_paths=b["top_paths"],
        )
        for b in ranked
    ]


async def list_codex_module_edges(
    db: aiosqlite.Connection,
    *,
    project: str = "marvisx",
    module_slugs: list[str],
    limit: int = 200,
) -> list[CodexModuleEdge]:
    """Aggregate function-to-function edges UP to module-to-module edges.

    Walks `graph_edges` of relation ∈ (calls, imports, depends_on, mentions)
    and lifts each endpoint to its module bucket via the same _bucket_slug
    helper used for modules. Edges within the same module are dropped.
    Sums weights per (src_module, dst_module, relation).
    """
    if not module_slugs:
        return []
    slug_set = set(module_slugs)
    async with db.execute(
        """
        SELECT e.source_id, e.target_id, e.relation,
               sn.file_path, sn.qualified_name, sn.type,
               tn.file_path, tn.qualified_name, tn.type
          FROM graph_edges e
          JOIN graph_nodes sn ON sn.id = e.source_id
          JOIN graph_nodes tn ON tn.id = e.target_id
         WHERE e.relation IN ('calls','imports','depends_on','mentions')
           AND (e.valid_until IS NULL OR e.valid_until > datetime('now'))
           AND sn.deprecated_at IS NULL
           AND tn.deprecated_at IS NULL
           AND (sn.project_id = ? OR sn.project_id IS NULL)
         LIMIT 50000
        """,
        (project,),
    ) as cur:
        rows = await cur.fetchall()

    weights: dict[tuple[str, str, str], int] = {}
    for src_id, tgt_id, rel, s_fp, s_qn, s_ty, t_fp, t_qn, t_ty in rows:
        s_slug = _bucket_slug(s_fp, s_qn, s_ty or "function")
        t_slug = _bucket_slug(t_fp, t_qn, t_ty or "function")
        if not s_slug or not t_slug or s_slug == t_slug:
            continue
        if s_slug not in slug_set or t_slug not in slug_set:
            continue
        key = (s_slug, t_slug, rel)
        weights[key] = weights.get(key, 0) + 1

    # Sort by weight DESC, take top `limit` to keep the canvas readable.
    ranked = sorted(weights.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]
    return [
        CodexModuleEdge(
            source=src,
            target=tgt,
            relation=rel,
            weight=w,
            hot=w >= 10,
        )
        for (src, tgt, rel), w in ranked
    ]


async def list_codex_modules_with_edges(
    db: aiosqlite.Connection,
    *,
    project: str = "marvisx",
    limit: int = 24,
) -> tuple[list[CodexModuleAggregate], list[CodexModuleEdge]]:
    """Convenience wrapper: modules + edges in one call, with degree filled in.

    Computes the edges first (so we know each module's degree), then enriches
    the module aggregate before returning. Used by the /codex-modules
    endpoint to avoid two round-trips.
    """
    modules = await list_codex_modules(db, project=project, limit=limit)
    slugs = [m.slug for m in modules]
    edges = await list_codex_module_edges(db, project=project, module_slugs=slugs)
    degree_by_slug: dict[str, int] = {s: 0 for s in slugs}
    for e in edges:
        degree_by_slug[e.source] = degree_by_slug.get(e.source, 0) + e.weight
        degree_by_slug[e.target] = degree_by_slug.get(e.target, 0) + e.weight
    enriched = [
        CodexModuleAggregate(
            slug=m.slug,
            cluster=m.cluster,
            label=m.label,
            function_count=m.function_count,
            file_count=m.file_count,
            degree=degree_by_slug.get(m.slug, 0),
            top_functions=m.top_functions,
            top_paths=m.top_paths,
            semantic_label=m.semantic_label,
            ratified=m.ratified,
            drift=m.drift,
        )
        for m in modules
    ]
    return enriched, edges


def _bucket_slug(file_path: str | None, qualified_name: str | None, node_type: str) -> str:
    """Pick a module slug from file_path; fall back to qualified_name.

    For function nodes without file_path we treat the qualified name as a
    dotted module path (e.g. `api.services.foo.bar`), drop the last
    segment (the function name itself), and feed the rest through the same
    slash-based bucketizer. For file nodes the qualified_name is usually
    already the file path.
    """
    if file_path:
        return _module_slug_from_path(file_path)
    if not qualified_name:
        return ""
    qn = qualified_name
    # Heuristic: dotted form for Python functions ('api.services.foo.bar'),
    # slash form for TS or already-canonical paths.
    if "/" in qn:
        return _module_slug_from_path(qn)
    parts = qn.split(".")
    if node_type == "function" and len(parts) > 1:
        parts = parts[:-1]  # drop the function name
    if not parts:
        return ""
    pseudo_path = "/".join(parts)
    return _module_slug_from_path(pseudo_path)


_FILE_EXT_RE = re.compile(r"\.(py|pyi|ts|tsx|jsx|js|mjs|cjs|sql|md|yaml|yml|json|toml|html|css|sh)$", re.IGNORECASE)


def _module_slug_from_path(path: str) -> str:
    """Take the first 2 segments of a path as the module slug.

    `api/services/auth/oauth.py` -> `api/services`
    `console/src/components/graph` -> `console/src`

    Single-file paths whose 2nd segment is a real file (has extension)
    collapse to the 1st segment so we don't end up with a planet per
    script. `scripts/heypocket_sync.py` -> `scripts`,
    `tests/test_foo.py` -> `tests`.

    Top-level files fall into `root` so they don't pollute the canvas.
    """
    if "/" not in path:
        return "root"
    parts = path.split("/", 2)
    if len(parts) < 2:
        return parts[0]
    # Collapse "<dir>/<file.ext>" -> "<dir>" so single-file modules don't
    # create their own planet in the macro canvas.
    if _FILE_EXT_RE.search(parts[1]) and (len(parts) < 3 or not parts[2]):
        return parts[0]
    return f"{parts[0]}/{parts[1]}"


def _module_label(slug: str) -> str:
    """Human-readable label for a module slug — last segment of the path."""
    if "/" not in slug:
        return slug
    return slug.rsplit("/", 1)[-1]


@dataclass(frozen=True)
class CodexFunctionItem:
    node_id: str
    qualified_name: str
    file_path: str | None
    line_number: int | None
    touch_count_7d: int
    touch_count_30d: int


_MODULE_EXT_RE = re.compile(r"\.(py|pyi|ts|tsx|js|jsx|mjs|cjs)$")


def _qname_prefix_for_module(module: str) -> str:
    """Turn a filesystem module slug into the dotted qname prefix.

    `api/security.py` -> `api.security`, `api/services` -> `api.services`.
    Strips the language extension before swapping `/` for `.` so the prefix
    matches the canonical `qualified_name` (always dot-separated, no ext).
    """
    return _MODULE_EXT_RE.sub("", module).replace("/", ".")


async def list_codex_functions(
    db: aiosqlite.Connection,
    *,
    project: str = "marvisx",
    module: str,
    limit: int = 200,
) -> list[CodexFunctionItem]:
    """Return functions whose file_path or qualified_name starts with the
    module slug. Sorted by recent touch_count to surface the busy ones.

    Handles two module shapes consistently with `list_codex_modules`:
      - file-as-module (`api/security.py`)  → matches file_path exactly
        AND qualified_name LIKE `api.security.%`
      - dir-as-module  (`api/services`)     → matches file_path LIKE
        `api/services/%` AND qualified_name LIKE `api.services.%`
    """
    if not module:
        return []
    qname_prefix = _qname_prefix_for_module(module)
    like_file_under = f"{module}/%"
    like_qname = f"{qname_prefix}.%"
    async with db.execute(
        """
        SELECT id, qualified_name, file_path, line_number,
               touch_count_7d, touch_count_30d
          FROM graph_nodes
         WHERE type = 'function'
           AND deprecated_at IS NULL
           AND (project_id = ? OR project_id IS NULL)
           AND (
                file_path = ?
             OR file_path LIKE ?
             OR qualified_name LIKE ?
           )
         ORDER BY touch_count_7d DESC, touch_count_30d DESC, qualified_name ASC
         LIMIT ?
        """,
        (project, module, like_file_under, like_qname, limit),
    ) as cur:
        rows = await cur.fetchall()
    return [
        CodexFunctionItem(
            node_id=r[0],
            qualified_name=r[1] or "",
            file_path=r[2],
            line_number=r[3],
            touch_count_7d=r[4] or 0,
            touch_count_30d=r[5] or 0,
        )
        for r in rows
    ]
