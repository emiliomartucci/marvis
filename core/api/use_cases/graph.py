# v1.0.0 - 2026-05-27 - S1 F1.8: graph use_cases extracted from router (Knowledge Graph)
"""Knowledge-Graph use_cases — pure domain logic, transport-agnostic (no ``fastapi``).

Sibling of :mod:`core.api.routers.graph` in the S1 "collapse runtime" refactor.
Each query operation is a pure async function ``(ctx, db, *typed_args) -> <DTO>``;
the router is a thin adapter that resolves identity, enforces visibility, applies
rate limits, and maps :class:`ServiceError` -> ``HTTPException``.

Four router-specific decisions (deviations / specialisations of the template):

DECISION A — kg services that drag ``fastapi`` are kept out of this module.
    ``graph_service`` / ``graph_ranker`` are fastapi-free → module-top imports.
    But ``graph_cosmo_service`` (cosmo) and ``services.share_links``
    (share_function URL) transitively import ``fastapi`` and live entirely in the
    adapter. ``visibility.filter_visible_edges`` (overview macro RBAC) also imports
    ``fastapi`` AND is test-pinned to the ``routers.graph`` namespace
    (``patch("api.routers.graph.filter_visible_edges")`` in
    test_overview_rbac_hides_edges) — so it is INJECTED into ``graph_overview`` as
    a callable resolved at the adapter boundary (DECISION 1 style), never imported
    here. To keep THIS module fastapi-free at import time (the import-linter
    contract + the smoke test assert), no fastapi-importing module is referenced.

DECISION B — visibility ENFORCEMENT stays in the adapter (graph deviation from
    the template's DECISION 1). ``get_visible_projects`` is resolved in the
    router (DECISION 1 resolves at the boundary), but graph endpoints enforce
    with DIFFERENT transport outcomes that are part of the API contract and are
    test-pinned to a *plain-string* detail body:
      * project-scoped reads (neighbors/hotspots/impact/context/pattern) →
        ``403 "Project not accessible"`` (test_kg_security_h2_h3 asserts the
        substring "not accessible").
      * oracle-avoidance reads (resolve / overview module / orphans) →
        ``404 "Not found"`` on a visibility miss.
    Routing those through ``ServiceError`` + the structured ``to_http`` body
    would change every response body, and the test patches
    ``api.routers.graph.get_visible_projects`` (so the call must stay in the
    router namespace). The check therefore lives where it fires today; the
    use_case does the pure data work.

DECISION C — the deep/share transport layer stays in the adapter.
    ``share_function`` couples a *workspace-share* side effect (signed URL via
    ``share_links`` + ``enforce_workspace_share_role``, on the write pool) with a
    pure KG-context bundle. URL generation + role enforcement are transport
    concerns kept in the adapter; the pure pieces (include parsing, node lookup
    error mapping, preview/hotspot/neighbors/context assembly) live here.

DECISION D — domain errors carry the LEGACY plain-string body.
    Today every graph ``HTTPException`` uses a rich human-readable string detail
    (documented in the endpoint docstrings, part of the agent-facing contract),
    NOT the structured ``{code,message}`` shape. The use_case raises
    :class:`ServiceError` whose ``message`` is byte-identical to the legacy
    detail; the adapter re-raises it as ``HTTPException(status, message)`` (the
    ``to_http_legacy`` helper) so bodies are unchanged.
"""
from __future__ import annotations

import json as json_mod
import re
from datetime import datetime, timezone

import aiosqlite

from core.api.models.graph import GraphCapabilities, RankType
from core.api.models.graph_ux import (
    HotspotItem,
    KGClaim,
    LandingBundle,
    OrphanFile,
    OrphanSubCluster,
    OrphansBundle,
    OverviewBundle,
    OverviewEdge,
    OverviewNode,
    PinOut,
    RecentItem,
    ResolveOut,
)
from core.api.services import graph_ranker, graph_service
from core.api.use_cases._context import CallerContext
from core.api.use_cases._errors import NotFoundError, ServiceError, ValidationError

class _PinWriteFailedError(ServiceError):
    """500 — post-upsert fetch miss (should never happen). Plain-string body."""

    http_status = 500


# ---------------------------------------------------------------------------
# Domain constants (pure — moved verbatim from the router)
# ---------------------------------------------------------------------------

QUALIFIED_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9_.\-]+$")
MAX_QUALIFIED_NAME_LEN = 256

SHARE_FUNCTION_INCLUDES: frozenset[str] = frozenset(
    {"preview", "neighbors", "context", "hotspot"}
)
DEFAULT_SHARE_FUNCTION_INCLUDE: tuple[str, ...] = (
    "preview", "neighbors", "context", "hotspot"
)
PREVIEW_LINES: int = 20
NEIGHBORS_LIMIT: int = 10
SHARE_FUNCTION_DEFAULT_HOURS: int = 24

# Node ID regex for path params (same as PinIn field pattern)
_NODE_ID_RE = re.compile(r"^[a-z]+:[a-z]+:.+$")

# Orphan sub-cluster color map (deterministic by folder prefix)
_ORPHAN_COLORS: dict[str, str] = {
    "docs": "#D4A017",      # ocra
    "memory": "#7B2D8B",    # viola
    "scripts": "#17A2B8",   # ciano
    "kb": "#556B2F",        # oliva
    "tests": "#FF69B4",     # rosa
    "output": "#808080",    # grey
    "data": "#20B2AA",      # seafoam
}
_ORPHAN_COLOR_DEFAULT = "#A0A0A0"  # neutral


# ---------------------------------------------------------------------------
# Pure helpers (moved verbatim from the router)
# ---------------------------------------------------------------------------


def parse_include_csv(include: str | None) -> list[str]:
    """Parse CSV `include` param into validated list of blocks.

    Empty / None → default (all four blocks). Unknown tokens → ValueError.
    """
    if include is None or not include.strip():
        return list(DEFAULT_SHARE_FUNCTION_INCLUDE)
    tokens = [t.strip() for t in include.split(",") if t.strip()]
    if not tokens:
        return list(DEFAULT_SHARE_FUNCTION_INCLUDE)
    unknown = [t for t in tokens if t not in SHARE_FUNCTION_INCLUDES]
    if unknown:
        raise ValueError(
            f"Unknown include tokens: {unknown!r}. "
            f"Allowed: {sorted(SHARE_FUNCTION_INCLUDES)}"
        )
    # Preserve order, dedupe.
    seen: set[str] = set()
    out: list[str] = []
    for t in tokens:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


def extract_hotspot(node_row: aiosqlite.Row) -> dict:
    """Pull touch counters + top 3 authors from a graph_nodes row."""
    authors_raw = node_row["touch_authors"] if "touch_authors" in node_row.keys() else None
    try:
        authors = json_mod.loads(authors_raw) if authors_raw else []
    except (TypeError, ValueError):
        authors = []
    if not isinstance(authors, list):
        authors = []
    return {
        "touch_count_total": node_row["touch_count_total"] if "touch_count_total" in node_row.keys() else 0,
        "touch_count_30d": node_row["touch_count_30d"] if "touch_count_30d" in node_row.keys() else 0,
        "touch_count_7d": node_row["touch_count_7d"] if "touch_count_7d" in node_row.keys() else 0,
        "touch_last_at": node_row["touch_last_at"] if "touch_last_at" in node_row.keys() else None,
        "top_authors": authors[:3],
    }


def parse_db_datetime(value: str | None) -> datetime:
    """Parse a SQLite datetime string to UTC-aware datetime.

    SQLite stores timestamps as TEXT in ISO8601 format (various flavors).
    This helper normalises them to UTC-aware Python datetime objects.
    """
    if not value:
        return datetime.now(timezone.utc)
    try:
        # Handle both 'Z' suffix and offset-naive (assumed UTC)
        s = value.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, AttributeError):
        return datetime.now(timezone.utc)


def orphan_color(folder: str) -> str:
    """Return deterministic color for a folder based on its prefix."""
    for prefix, color in _ORPHAN_COLORS.items():
        if folder == prefix or folder.startswith(prefix + "/"):
            return color
    return _ORPHAN_COLOR_DEFAULT


# ---------------------------------------------------------------------------
# neighbors / hotspots / impact / context / pattern (read queries)
# ---------------------------------------------------------------------------


def _git_head_sha(repo_path) -> str | None:
    """Best-effort current HEAD sha (abbreviated) for a repo. None on any failure."""
    import shutil
    import subprocess

    git = shutil.which("git")
    if not git or repo_path is None:
        return None
    try:
        out = subprocess.run(
            [git, "-C", str(repo_path), "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None


async def compute_index_freshness(
    db: aiosqlite.Connection, *, node_id: str, project: str | None = None
) -> dict:
    """Freshness/coverage signal for the KG slice a graph query is reading (#13).

    graph_impact / graph_neighbors otherwise return an authoritative-looking blast
    radius with no hint that the underlying index is stale or partial, so a stale
    index silently under-reports the callers of a symbol. This surfaces, for the
    queried node's project: indexed node count, the representative indexed git sha,
    the project's current HEAD, the last-indexed timestamp, and a best-effort
    ``stale`` + ``reason``. A read NEVER mutates here — drift returns an actionable
    ``hint`` (and, under ``MARVIS_GRAPH_AUTOREINDEX_ON_DRIFT``, an explicit
    ``reindex_command``); re-indexing the KG is a heavy repo re-parse and stays an
    explicit operation.
    """
    from core.api.config import settings

    proj = project
    if proj is None:
        cur = await db.execute(
            "SELECT project_id FROM graph_nodes WHERE id = ? LIMIT 1", (node_id,)
        )
        row = await cur.fetchone()
        proj = (row[0] if row else None) or None

    result: dict = {
        "project": proj,
        "indexed_node_count": 0,
        "indexed_git_sha": None,
        "head_git_sha": None,
        "last_indexed_at": None,
        "stale": False,
        "reason": "no_project",
    }
    if not proj:
        return result

    cur = await db.execute(
        "SELECT COUNT(*) AS total, "
        "       COUNT(NULLIF(last_modified_git_sha, '')) AS with_sha, "
        "       MAX(touch_last_at) AS last_indexed "
        "FROM graph_nodes WHERE project_id = ?",
        (proj,),
    )
    agg = await cur.fetchone()
    total = (agg["total"] if agg else 0) or 0
    with_sha = (agg["with_sha"] if agg else 0) or 0
    result["indexed_node_count"] = total
    result["last_indexed_at"] = agg["last_indexed"] if agg else None

    if total == 0:
        result["reason"] = "not_indexed"
        result["stale"] = True
        result["hint"] = f"run `marvis project index {proj}` — the graph has no nodes for this project"
        _maybe_mark_reindex(result, proj, settings)
        return result

    try:
        from core.api.routers.projects import _find_git_path

        repo_path = _find_git_path(proj)
    except Exception:
        repo_path = None

    if repo_path is None:
        # Artifact-only / non-git (`work`) project: no HEAD to drift from.
        result["reason"] = "not_git_linked"
        return result

    cur = await db.execute(
        "SELECT last_modified_git_sha FROM graph_nodes "
        "WHERE project_id = ? AND last_modified_git_sha IS NOT NULL "
        "AND last_modified_git_sha != '' ORDER BY touch_last_at DESC LIMIT 1",
        (proj,),
    )
    sha_row = await cur.fetchone()
    indexed_sha = sha_row[0] if sha_row else None
    result["indexed_git_sha"] = indexed_sha

    head_sha = _git_head_sha(repo_path)
    result["head_git_sha"] = head_sha

    if with_sha == 0 or not indexed_sha:
        result["reason"] = "sha_unpopulated"
        result["stale"] = True
        result["hint"] = (
            f"the graph for {proj} carries no commit provenance (not git-linked at index time) — "
            f"`graph_impact` may under-report; run `marvis project index {proj}`"
        )
    elif head_sha is None:
        result["reason"] = "head_unknown"  # cannot read HEAD → do not claim stale
    elif indexed_sha == head_sha or head_sha.startswith(indexed_sha) or indexed_sha.startswith(head_sha):
        result["reason"] = "fresh"
    else:
        result["reason"] = "behind_head"
        result["stale"] = True
        result["hint"] = (
            f"the graph for {proj} is indexed at {indexed_sha} but HEAD is {head_sha} — "
            f"blast radius may be incomplete; run `marvis project index {proj}`"
        )

    _maybe_mark_reindex(result, proj, settings)
    return result


def _maybe_mark_reindex(result: dict, project: str, settings) -> None:
    """Opt-in (#13): mark the reindex as an explicit next action. Never executes it."""
    if result.get("stale") and settings.graph_autoreindex_on_drift:
        result["auto_reindex"] = "suggested"
        result["reindex_command"] = f"marvis project index {project}"


def _neighbor_summary(
    raw: list,
    n_needs_review: int | None = None,
    *,
    needs_review_suppressed: bool = False,
) -> dict:
    """Pre-aggregated counts so the agent QUOTES numbers instead of re-counting the
    list by hand (#1a — the at-scale faithfulness fix: LLMs miscount raw lists, e.g.
    reporting 32 when the graph has 137). Computed from the already-fetched
    neighbours, no extra query.

    Fase 3 (de-noise): when ``needs_review_suppressed`` the per-neighbour flag was
    stripped as noise (index broadly stale, or the flagged majority stopped
    discriminating); the count is stated once here instead of decorating every row.
    """
    from collections import Counter

    by_relation: Counter = Counter()
    by_direction: Counter = Counter()
    nodes: set = set()
    for n in raw:
        if not isinstance(n, dict):
            continue
        rel = n.get("relation")
        if rel:
            by_relation[rel] += 1
        direction = n.get("direction")
        if direction:
            by_direction[direction] += 1
        nid = n.get("node_id") or n.get("id")
        if nid:
            nodes.add(nid)
    out: dict = {
        "total": len(raw),
        "distinct_nodes": len(nodes),
        "by_relation": dict(by_relation.most_common()),
        "by_direction": dict(by_direction),
        "note": "pre-aggregated — cite these counts, do not re-count the list",
    }
    if n_needs_review is not None:
        out["needs_review"] = n_needs_review
        if needs_review_suppressed:
            out["needs_review_note"] = (
                "index broadly stale or the flagged majority stopped discriminating "
                "— per-neighbour needs_review suppressed as noise; this is the aggregate count"
            )
    return out


# ---------------------------------------------------------------------------
# at-scale freshness (Fase A) — read-time needs_review + derivation.
# Behind MARVIS_TEMPORAL_MEMORY (settings.temporal_memory_enabled, default off):
# flag-off = byte-identical output (no fields added). The agent must SEE staleness
# at read time — post-hoc human review is too slow (the stale-doc failure mode).
# ---------------------------------------------------------------------------

_OBSERVED_RELATIONS = frozenset({"calls", "imports", "defines"})
# Days past which a human reviewer would want to re-confirm a fact of this kind —
# NOT a statistical cutpoint. Code churns fast (git-driven); learnings are slow.
# Tunable from telemetry on flag-rate per type.
_STALE_AFTER_DAYS = {
    "function": 7, "file": 7, "module": 7,
    "task": 30, "handoff": 30, "project": 30, "doc": 30, "pr": 30, "artifact": 30,
    "learning": 90,
}
_STALE_DEFAULT_DAYS = 30
_DERIVED_TIGHTEN = 0.5  # an inferred (derived) fact gets half the staleness budget


def _rel_of(n: dict) -> str | None:
    """Relation, tolerant of the flat shape and the nested-`edge` (prod) shape."""
    return n.get("relation") or (n.get("edge") or {}).get("relation")


def _derivation_of(rel: str | None) -> str:
    """observed = deterministic (code edge, trust by construction); derived =
    inferred (mentions/similar_to/etc) — deserves more skepticism."""
    return "observed" if rel in _OBSERVED_RELATIONS else "derived"


def _node_kind(n: dict) -> str:
    kind = n.get("type")
    if kind:
        return str(kind)
    nid = n.get("node_id") or n.get("id") or ""
    parts = nid.split(":")
    return parts[1] if len(parts) >= 3 else ""


def _parse_iso(ts):
    if not ts or not isinstance(ts, str):
        return None
    from datetime import datetime
    try:
        return datetime.fromisoformat(ts.strip().replace(" ", "T").replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _needs_review(n: dict, now) -> bool:
    """Binary stale flag from EXISTING timestamps. Load-bearing rule: a missing /
    unparseable / FUTURE timestamp is NEVER treated as fresh (Graphiti #1489)."""
    from datetime import timezone
    fr = n.get("freshness") or {}
    # Fase D: last_verified_at (Brain REINFORCE, Triage-approved) is the most
    # authoritative recency signal — a re-verified node is fresh even if its
    # mechanical observation timestamps are old. It joins the max() below.
    stamps = [t for t in (_parse_iso(fr.get("node_updated_at")),
                          _parse_iso(fr.get("edge_last_seen_at")),
                          _parse_iso(fr.get("node_last_verified_at"))) if t is not None]
    if not stamps:
        return True
    newest = max((t if t.tzinfo else t.replace(tzinfo=timezone.utc)) for t in stamps)
    if newest > now:
        return True
    budget = _STALE_AFTER_DAYS.get(_node_kind(n), _STALE_DEFAULT_DAYS)
    if _derivation_of(_rel_of(n)) == "derived":
        budget *= _DERIVED_TIGHTEN
    return (now - newest).total_seconds() / 86400.0 > budget


def _apply_freshness(raw: list) -> int:
    """Decorate each neighbour in-place with `derivation` + `needs_review`, strip
    the internal `freshness` block. Returns how many are needs_review."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    n_review = 0
    for n in raw:
        if not isinstance(n, dict):
            continue
        n["derivation"] = _derivation_of(_rel_of(n))
        flag = _needs_review(n, now)
        n["needs_review"] = flag
        n_review += 1 if flag else 0
        n.pop("freshness", None)
    return n_review


# Fraction of flagged neighbours above which the per-neighbour `needs_review`
# stops discriminating and becomes wallpaper (the S1/S2 regression: 127/200
# flagged drowns the task). A minority flag IS signal ("these 3 of 50 are old");
# a majority is just the global slice state, already in `freshness.stale`.
_NEEDS_REVIEW_NOISE_FRACTION = 0.5


def _gate_needs_review_noise(raw: list, n_needs_review: int, *, index_stale: bool) -> bool:
    """Fase 3 de-noise. The per-neighbour `needs_review` flag is signal only as a
    MINORITY hint on a fresh index. When the index is globally stale (coverage
    already flagged in `freshness`) or most neighbours are flagged, the flag is
    noise: strip it from every neighbour and let the summary state the count once.
    Returns True when suppressed. `derivation` is left intact (it is the edge kind,
    not a staleness alarm)."""
    total = len(raw)
    majority_flagged = total > 0 and (n_needs_review / total) > _NEEDS_REVIEW_NOISE_FRACTION
    if not (index_stale or majority_flagged):
        return False
    for n in raw:
        if isinstance(n, dict):
            n.pop("needs_review", None)
    return True


# ---------------------------------------------------------------------------
# answer-ready claims (behind MARVIS_KG_CLAIMS). The DB counts, the agent reports.
# Dependency = real ("X depends_on/imports/calls subject"); associative =
# weak ("X refers_to/mentions subject"), labeled `mentioned_by` and NEVER counted
# as a dependency (refers_to alone is 112k of mechanical noise). SQL lives in
# graph_service; here is pure assembly. See plan 2026-06-08-feat-answer-ready-claims.
# ---------------------------------------------------------------------------

_DEPENDENCY_RELATIONS = ("depends_on", "imports", "calls")
_ASSOCIATIVE_RELATIONS = ("refers_to", "mentions")
_CLAIM_SOURCE_SAMPLE = 10


def _claim_needs_review(node_type, last_seen_at, last_verified_at, relation, now) -> bool:
    """Reuse the Fase A budget on the claim subject: stale (or missing/future
    stamp) → review. last_verified_at (Brain re-confirmation) counts as freshest."""
    nb = {
        "type": node_type,
        "edge": {"relation": relation},
        "freshness": {
            "node_updated_at": last_seen_at,
            "edge_last_seen_at": None,
            "node_last_verified_at": last_verified_at,
        },
    }
    return _needs_review(nb, now)


async def _build_claims(
    db: aiosqlite.Connection,
    node_id: str,
    *,
    now=None,
    exclude_source_ids: tuple[str, ...] = (),
) -> list[dict]:
    """Server-computed, relation-typed claims for a subject node. Pure assembly
    over graph_service count/sample helpers — no SQL here. Returns model_dump()
    dicts (validated at construction). Empty list if the node is missing."""
    from datetime import datetime, timezone

    now = now or datetime.now(timezone.utc)
    trust = await graph_service.node_trust_fields(db, node_id)
    if trust is None:
        return []
    ntype = trust["type"]
    last_seen = trust["last_seen_at"]
    last_verified = trust["last_verified_at"]
    excl = tuple(exclude_source_ids)

    def _claim(kind: str, value: int, sources: list[str], rel: str) -> dict:
        return KGClaim(
            kind=kind,
            subject=node_id,
            value=value,
            sources=sources,
            sources_total=value,
            as_of=last_seen,
            verified=last_verified,
            needs_review=_claim_needs_review(ntype, last_seen, last_verified, rel, now),
        ).model_dump()

    claims: list[dict] = []

    # Dependency claims (real): emit only relations with a live in-edge count > 0.
    for rel in _DEPENDENCY_RELATIONS:
        n = await graph_service.count_in_edges_by_relation(
            db, node_id, rel, exclude_sources=excl
        )
        if n <= 0:
            continue
        srcs = await graph_service.sample_in_edge_sources(
            db, node_id, rel, limit=_CLAIM_SOURCE_SAMPLE, exclude_sources=excl
        )
        claims.append(_claim(f"dependents_{rel}", n, srcs, rel))

    # Associative contrast: one `mentioned_by` claim (refers_to + mentions),
    # labeled, NEVER a dependency — lets the agent see "mentioned_by 112k but
    # depends_on 2". sources_total = full associative count (>= len(sources)).
    assoc_total = 0
    for rel in _ASSOCIATIVE_RELATIONS:
        assoc_total += await graph_service.count_in_edges_by_relation(
            db, node_id, rel, exclude_sources=excl
        )
    if assoc_total > 0:
        srcs = await graph_service.sample_in_edge_sources(
            db, node_id, "refers_to", limit=_CLAIM_SOURCE_SAMPLE, exclude_sources=excl
        )
        claims.append(
            KGClaim(
                kind="mentioned_by",
                subject=node_id,
                value=assoc_total,
                sources=srcs,
                sources_total=assoc_total,
                as_of=last_seen,
                verified=last_verified,
                needs_review=_claim_needs_review(
                    ntype, last_seen, last_verified, "refers_to", now
                ),
            ).model_dump()
        )

    return claims


async def graph_neighbors(
    ctx: CallerContext,
    db: aiosqlite.Connection,
    *,
    node_id: str,
    relation: str | None = None,
    edge_types: list[str] | None = None,
    project: str | None = None,
    direction: str = "both",
    limit: int = 50,
    rank: RankType = "none",
    as_of: str | None = None,
) -> dict:
    """Return neighbours of a graph node, optionally ranked / time-travelled / cross-project.

    Visibility (project) is enforced by the adapter (DECISION B). 422 (ValidationError)
    on malformed inputs; 404 (NotFoundError) if the node does not exist at ``as_of``.
    """
    from core.api.config import settings
    with_freshness = settings.temporal_memory_enabled
    try:
        raw = await graph_service.get_neighbors_with_metadata(
            db,
            node_id=node_id,
            relation=relation,
            direction=direction,
            limit=limit,
            as_of=as_of,
            project=project,
            edge_types=edge_types,
            with_freshness=with_freshness,
            workspace_id=ctx.workspace_id or "ws_default",
        )
    except ValueError as e:
        raise ValidationError(code="invalid_request", message=str(e))

    if not raw:
        # Distinguish "node missing" from "node has no matching neighbours".
        # Use temporal-aware existence check so a node that was deprecated
        # pre-`as_of` is still reachable via time-travel.
        try:
            exists = await graph_service.node_exists_at(db, node_id, as_of=as_of)
        except ValueError as e:
            raise ValidationError(code="invalid_request", message=str(e))
        if not exists:
            raise NotFoundError(
                code="node_not_found",
                message=(
                    f"Node not found in the knowledge graph: {node_id!r}"
                    + (f" (at as_of={as_of!r})" if as_of else "")
                    + ". Reason: no row in graph_nodes with this id (and, if as_of was given, "
                    "not in the historical state at that timestamp). "
                    "Fix: (1) verify node_id format: '{lang}:{type}:{qualified_name}'; "
                    "(2) use mcp__marvis__search to find similar ids; "
                    "(3) if the code was recently added, run scripts/populate_knowledge_graph.py to index it."
                ),
            )

    response: dict = {
        "node_id": node_id,
        "neighbors": raw,
        "count": len(raw),
    }
    response["freshness"] = await compute_index_freshness(db, node_id=node_id, project=project)
    if with_freshness:
        n_review = _apply_freshness(raw)
        suppressed = _gate_needs_review_noise(
            raw, n_review, index_stale=bool((response["freshness"] or {}).get("stale"))
        )
        response["summary"] = _neighbor_summary(
            raw, n_needs_review=n_review, needs_review_suppressed=suppressed
        )
    else:
        response["summary"] = _neighbor_summary(raw)
    if as_of is not None:
        response["as_of"] = as_of

    if rank == "none":
        return response

    ranker_fn = graph_ranker.RANKERS.get(rank)
    if ranker_fn is None:
        # FastAPI's Literal typing already rejects unknown values with a 422,
        # but guard defensively for typos between router/registry.
        raise ValidationError(
            code="unknown_rank",
            message=(
                f"Unknown rank: {rank!r}. "
                f"Allowed values: {sorted(graph_ranker.RANKERS.keys())}. "
                "Fix: pick a registered ranker, or rank='none' to skip ranking."
            ),
        )

    ranked = await ranker_fn(db, raw)
    response["neighbors"] = ranked
    response["count"] = len(ranked)
    response["rank"] = rank
    return response


async def graph_hotspots(
    ctx: CallerContext,
    db: aiosqlite.Connection,
    *,
    window: str = "30d",
    limit: int = 20,
    type_filter: str = "file",
    project: str | None = None,
) -> dict:
    """Top N hotspot nodes ordered by touch count in the chosen window.

    Visibility enforced by the adapter (DECISION B). 422 on malformed inputs.
    """
    try:
        graph_service.validate_project(project)
        hotspots = await graph_service.get_hotspots(
            db,
            window=window,
            limit=limit,
            type_filter=type_filter,
            project=project,
        )
    except ValueError as e:
        raise ValidationError(code="invalid_request", message=str(e))

    return {
        "window": window,
        "type_filter": type_filter,
        "project": project,
        "hotspots": hotspots,
        "count": len(hotspots),
    }


async def graph_impact(
    ctx: CallerContext,
    db: aiosqlite.Connection,
    *,
    node_id: str,
    depth: int = 2,
    limit: int = 50,
    edge_types: list[str] | None = None,
    project: str | None = None,
) -> dict:
    """Reverse impact analysis for a graph node.

    Visibility enforced by the adapter (DECISION B). 422 on malformed inputs,
    404 if the node does not exist.
    """
    try:
        graph_service.validate_node_id(node_id)
        graph_service.validate_project(project)
        graph_service.validate_edge_types(edge_types)
    except ValueError as e:
        raise ValidationError(code="invalid_request", message=str(e))

    if not await graph_service.node_exists_at(db, node_id):
        raise NotFoundError(
            code="node_not_found",
            message=(
                f"Node not found in the knowledge graph: {node_id!r}. "
                "Reason: no row in graph_nodes with this id. "
                "Fix: (1) verify format '{lang}:{type}:{qualified_name}' "
                "(e.g. 'py:function:api.db.get_db'); "
                "(2) use mcp__marvis__search(q=<substring>) to find similar ids; "
                "(3) run scripts/populate_knowledge_graph.py if the code was recently added."
            ),
        )

    try:
        result = await graph_service.graph_impact(
            db, node_id=node_id, depth=depth, limit=limit,
            project=project, edge_types=edge_types,
        )
    except ValueError as e:
        raise ValidationError(code="invalid_request", message=str(e))
    if isinstance(result, dict):
        result["freshness"] = await compute_index_freshness(db, node_id=node_id, project=project)
        # answer-ready claims (behind MARVIS_KG_CLAIMS): server-computed,
        # relation-typed counts the agent reports instead of re-counting the raw
        # lists. ADDITIVE (old keys untouched → byte-identical when off) and
        # fail-soft (a claims error never breaks the impact response). graph_impact
        # carries no per-neighbour needs_review, so claims[].needs_review is the
        # sole freshness signal here — no overlapping surface to suppress.
        from core.api.config import settings
        if settings.kg_claims_enabled:
            try:
                result["claims"] = await _build_claims(db, node_id)
            except Exception:
                pass  # additive + optional: omit claims, keep the impact response
    return result


async def graph_context(
    ctx: CallerContext,
    db: aiosqlite.Connection,
    *,
    node_id: str,
    per_category_limit: int = 5,
    project: str | None = None,
) -> dict:
    """Trace the rationale chain for a node.

    Visibility enforced by the adapter (DECISION B). 422 on malformed inputs,
    404 if the node does not exist.
    """
    try:
        graph_service.validate_node_id(node_id)
        graph_service.validate_project(project)
    except ValueError as e:
        raise ValidationError(code="invalid_request", message=str(e))

    if not await graph_service.node_exists_at(db, node_id):
        raise NotFoundError(
            code="node_not_found",
            message=(
                f"Node not found in the knowledge graph: {node_id!r}. "
                "Reason: no row in graph_nodes with this id. "
                "Fix: (1) verify format '{lang}:{type}:{qualified_name}' "
                "(e.g. 'py:function:api.db.get_db'); "
                "(2) use mcp__marvis__search(q=<substring>) to find similar ids; "
                "(3) run scripts/populate_knowledge_graph.py if the code was recently added."
            ),
        )

    try:
        return await graph_service.graph_context(
            db, node_id=node_id, per_category_limit=per_category_limit,
            project=project,
        )
    except ValueError as e:
        raise ValidationError(code="invalid_request", message=str(e))


async def graph_pattern(
    ctx: CallerContext,
    db: aiosqlite.Connection,
    *,
    scope: str,
    limit: int = 20,
    project: str | None = None,
) -> dict:
    """Learnings applicable to a scope (file, module, or specific function).

    Visibility enforced by the adapter (DECISION B). 422 on malformed inputs.
    """
    try:
        graph_service.validate_project(project)
        return await graph_service.graph_pattern(
            db, scope=scope, limit=limit, project=project,
        )
    except ValueError as e:
        raise ValidationError(code="invalid_request", message=str(e))


# ---------------------------------------------------------------------------
# capabilities — KG schema metadata
# ---------------------------------------------------------------------------


async def get_capabilities(
    ctx: CallerContext,
    db: aiosqlite.Connection,
) -> GraphCapabilities:
    """Returns valid edge_types, node_kinds, node_prefixes + schema_version.

    Security: mcp_version NOT exposed (no fingerprinting).
    """
    # graph_service is fastapi-free; EDGE_TYPES/NODE_KINDS/NODE_PREFIXES are
    # module constants — imported at module top via ``graph_service``.
    cursor = await db.execute("SELECT MAX(version) FROM schema_versions")
    row = await cursor.fetchone()
    schema_version = row[0] if row and row[0] is not None else None
    return GraphCapabilities(
        edge_types=list(graph_service.EDGE_TYPES),
        node_kinds=list(graph_service.NODE_KINDS),
        node_prefixes=list(graph_service.NODE_PREFIXES),
        schema_version=schema_version,
    )


# ---------------------------------------------------------------------------
# share_function — pure pieces (DECISION C)
# ---------------------------------------------------------------------------


def validate_qualified_name(qualified_name: str) -> str:
    """Validate the share_function qualified_name. Raises ValidationError (422)."""
    if not qualified_name or len(qualified_name) > MAX_QUALIFIED_NAME_LEN:
        raise ValidationError(
            code="invalid_qualified_name",
            message=(
                f"qualified_name too long or empty (got length {len(qualified_name)}, "
                f"max {MAX_QUALIFIED_NAME_LEN}). "
                "Expected: dotted-lowercase Python path like 'api.db.get_write_db'. "
                "Fix: pass a non-empty name under the limit; check for accidentally concatenated values."
            ),
        )
    if not QUALIFIED_NAME_PATTERN.match(qualified_name):
        raise ValidationError(
            code="invalid_qualified_name",
            message=(
                f"Invalid qualified_name: {qualified_name!r}. "
                f"Expected pattern: {QUALIFIED_NAME_PATTERN.pattern} "
                "(dotted-lowercase tokens, e.g. 'api.db.get_db' or 'api.services.graph_service.node_exists'). "
                "Fix: use the Python import path, not a file path — no slashes, no uppercase, no trailing '()'."
            ),
        )
    return qualified_name


async def lookup_function_node(
    db: aiosqlite.Connection,
    *,
    qualified_name: str,
) -> aiosqlite.Row:
    """Look up the live ``type='function'`` node row for a qualified_name.

    Raises NotFoundError (404) when no live node matches or when the matched
    node has no recorded file_path.
    """
    db.row_factory = aiosqlite.Row
    cur = await db.execute(
        "SELECT id, type, name, qualified_name, file_path, line_number, "
        "touch_count_total, touch_count_7d, touch_count_30d, "
        "touch_authors, touch_last_at "
        "FROM graph_nodes "
        "WHERE qualified_name = ? AND type = 'function' "
        "AND deprecated_at IS NULL "
        "LIMIT 1",
        (qualified_name,),
    )
    node_row = await cur.fetchone()
    if node_row is None:
        raise NotFoundError(
            code="function_not_found",
            message=(
                f"Function not found in graph: {qualified_name!r}. "
                "Reason: no graph_nodes row with type='function' + this qualified_name (live view excludes deprecated nodes). "
                "Fix: (1) check for typos in the dotted path (e.g. 'api.db.get_db' not 'api.db.get_DB'); "
                "(2) search alternates with mcp__marvis__search(q=<name substring>); "
                "(3) re-index with scripts/populate_knowledge_graph.py if the function is freshly added; "
                "(4) query with as_of=<past timestamp> if the function was recently deprecated."
            ),
        )
    if not node_row["file_path"]:
        raise NotFoundError(
            code="function_no_file_path",
            message=(
                f"Function {qualified_name!r} has no file_path recorded in graph_nodes. "
                "Reason: the node was indexed without a source file (legacy/partial import). "
                "Fix: re-run scripts/populate_knowledge_graph.py with the ast_parser to fill in file_path, "
                "or use mcp__marvis__share_file if you already know the file manually."
            ),
        )
    return node_row


async def build_share_function_blocks(
    db: aiosqlite.Connection,
    *,
    node_id: str,
    node_row: aiosqlite.Row,
    include_list: list[str],
) -> dict:
    """Assemble the optional KG-context blocks (neighbors/context/hotspot).

    Pure data assembly (preview is added by the adapter, which owns the disk read
    via ``share_links.validate_repo_path``). 422 on internal graph inconsistency.
    """
    blocks: dict = {}

    if "neighbors" in include_list:
        try:
            raw = await graph_service.get_neighbors_with_metadata(
                db,
                node_id=node_id,
                direction="incoming",
                limit=NEIGHBORS_LIMIT,
            )
            ranker_fn = graph_ranker.RANKERS.get("suspect_write")
            if ranker_fn is not None:
                ranked = await ranker_fn(db, raw)
            else:
                ranked = raw
            blocks["neighbors"] = {
                "count": len(ranked),
                "rank": "suspect_write",
                "items": ranked,
            }
        except ValueError as e:
            # Internal consistency error — surface rather than swallow.
            raise ValidationError(code="invalid_request", message=str(e))

    if "context" in include_list:
        try:
            blocks["context"] = await graph_service.graph_context(
                db, node_id=node_id, per_category_limit=5,
            )
        except ValueError as e:
            raise ValidationError(code="invalid_request", message=str(e))

    if "hotspot" in include_list:
        blocks["hotspot"] = extract_hotspot(node_row)

    return blocks


# ---------------------------------------------------------------------------
# P2 UX — landing / list_pins / resolve / overview / orphans (read queries)
# ---------------------------------------------------------------------------


async def graph_landing(
    ctx: CallerContext,
    db: aiosqlite.Connection,
    *,
    workspace_id: str,
    user_id: str,
    hotspots_cached: list[HotspotItem] | None = None,
    recent_cached: list[RecentItem] | None = None,
    pins_cached: list[PinOut] | None = None,
) -> tuple[LandingBundle, list[HotspotItem], list[RecentItem], list[PinOut]]:
    """Aggregated landing bundle: top-10 hotspots (30d) + last-20 recent + saved pins.

    The TTLCache lives in the adapter (transport). The adapter passes any cached
    slices in; this function computes the misses and returns the bundle PLUS the
    (possibly freshly-computed) slices so the adapter can repopulate its cache.
    """
    db.row_factory = aiosqlite.Row

    # --- Hotspots slice ---
    hotspots = hotspots_cached
    if hotspots is None:
        cur = await db.execute(
            """
            SELECT id, type, name, qualified_name,
                   touch_count_30d, touch_authors
            FROM graph_nodes
            WHERE deprecated_at IS NULL
            ORDER BY touch_count_30d DESC, touch_last_at DESC
            LIMIT 10
            """
        )
        rows = await cur.fetchall()
        hotspots = []
        for r in rows:
            try:
                authors = json_mod.loads(r["touch_authors"]) if r["touch_authors"] else []
            except (TypeError, ValueError):
                authors = []
            hotspots.append(HotspotItem(
                node_id=r["id"],
                label=r["qualified_name"] or r["name"],
                kind=r["type"],
                touch_count=r["touch_count_30d"] or 0,
                authors=authors[:3],
            ))

    # --- Recent artifacts slice ---
    recent = recent_cached
    if recent is None:
        cur = await db.execute(
            """
            SELECT id, type, name, created_at
            FROM graph_nodes
            WHERE type IN ('commit', 'pr', 'task', 'handoff')
              AND deprecated_at IS NULL
            ORDER BY created_at DESC
            LIMIT 20
            """
        )
        rows = await cur.fetchall()
        recent = []
        for r in rows:
            kind_val = r["type"]
            if kind_val not in ("commit", "pr", "task", "handoff"):
                continue
            recent.append(RecentItem(
                kind=kind_val,  # type: ignore[arg-type]
                node_id=r["id"],
                label=r["name"],
                at=parse_db_datetime(r["created_at"]),
            ))

    # --- Pins slice (user-scoped) ---
    saved = pins_cached
    if saved is None:
        cur = await db.execute(
            """
            SELECT p.node_id, p.pinned_at, p.note,
                   n.deprecated_at AS node_deprecated_at
            FROM kg_pins p
            JOIN graph_nodes n ON n.id = p.node_id
            WHERE p.user_id = ?
              AND n.deprecated_at IS NULL
            ORDER BY p.pinned_at DESC
            """,
            (user_id,),
        )
        rows = await cur.fetchall()
        saved = [
            PinOut(
                node_id=r["node_id"],
                pinned_at=parse_db_datetime(r["pinned_at"]),
                note=r["note"],
                is_stale=False,
            )
            for r in rows
        ]

    bundle = LandingBundle(hotspots=hotspots, recent=recent, saved_nodes=saved)
    return bundle, hotspots, recent, saved


async def list_graph_pins(
    ctx: CallerContext,
    db: aiosqlite.Connection,
    *,
    user_id: str,
) -> list[PinOut]:
    """List all pins for the current user, ordered by pinned_at DESC.

    Pins on soft-deleted nodes are excluded (JOIN graph_nodes WHERE deprecated_at IS NULL).
    """
    db.row_factory = aiosqlite.Row
    cur = await db.execute(
        """
        SELECT p.node_id, p.pinned_at, p.note
        FROM kg_pins p
        JOIN graph_nodes n ON n.id = p.node_id
        WHERE p.user_id = ?
          AND n.deprecated_at IS NULL
        ORDER BY p.pinned_at DESC
        """,
        (user_id,),
    )
    rows = await cur.fetchall()
    return [
        PinOut(
            node_id=r["node_id"],
            pinned_at=parse_db_datetime(r["pinned_at"]),
            note=r["note"],
            is_stale=False,
        )
        for r in rows
    ]


async def create_graph_pin(
    ctx: CallerContext,
    db: aiosqlite.Connection,
    *,
    workspace_id: str,
    user_id: str,
    node_id: str,
    note: str | None,
) -> PinOut:
    """Upsert a node pin for the current user.

    Idempotent: POST twice with the same node_id → single row, updates `note`.
    Commits the write (the write pool documents "caller must commit"). The
    adapter is responsible for cache invalidation (transport).

    404 (NotFoundError) when the node does not exist / is soft-deleted.
    """
    # Verify the node exists (not soft-deleted)
    db.row_factory = aiosqlite.Row
    cur = await db.execute(
        "SELECT id FROM graph_nodes WHERE id = ? AND deprecated_at IS NULL LIMIT 1",
        (node_id,),
    )
    node_row = await cur.fetchone()
    if node_row is None:
        raise NotFoundError(
            code="node_not_found",
            message=(
                f"Node not found: {node_id!r}. "
                "Either the node does not exist or has been soft-deleted. "
                "Fix: verify node_id format and that the node is in the live graph."
            ),
        )

    # UPSERT — ON CONFLICT updates note and pinned_at
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%f") + "Z"
    await db.execute(
        """
        INSERT INTO kg_pins (workspace_id, user_id, node_id, pinned_at, note)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(workspace_id, user_id, node_id) DO UPDATE SET
            note = excluded.note,
            pinned_at = excluded.pinned_at
        """,
        (workspace_id, user_id, node_id, now, note),
    )
    # get_write_db() documents "caller must commit" — flush WAL immediately so
    # the write is visible to the read-pool without waiting for periodic flush.
    await db.commit()

    # Fetch back the stored row
    db.row_factory = aiosqlite.Row
    cur = await db.execute(
        "SELECT node_id, pinned_at, note FROM kg_pins WHERE user_id = ? AND node_id = ? LIMIT 1",
        (user_id, node_id),
    )
    row = await cur.fetchone()
    if row is None:
        # Should not happen after a successful upsert. Legacy router raised
        # HTTPException(500, "Pin write succeeded but fetch failed") — preserve
        # the status (500) and plain-string body via a ServiceError subclass.
        raise _PinWriteFailedError(
            code="pin_write_failed",
            message="Pin write succeeded but fetch failed",
        )

    return PinOut(
        node_id=row["node_id"],
        pinned_at=parse_db_datetime(row["pinned_at"]),
        note=row["note"],
        is_stale=False,
    )


async def delete_graph_pin(
    ctx: CallerContext,
    db: aiosqlite.Connection,
    *,
    user_id: str,
    node_id: str,
) -> dict:
    """Remove a pin for the current user.

    422 (ValidationError) on malformed node_id; 404 (NotFoundError) if the pin
    does not exist for this user. Commits the delete. The adapter invalidates
    the pins cache (transport).
    """
    # Validate node_id format
    if not _NODE_ID_RE.match(node_id):
        raise ValidationError(
            code="invalid_node_id",
            message=f"Invalid node_id format: {node_id!r}. Expected pattern: prefix:kind:slug.",
        )

    # Check existence first (to return proper 404)
    db.row_factory = aiosqlite.Row
    cur = await db.execute(
        "SELECT id FROM kg_pins WHERE user_id = ? AND node_id = ? LIMIT 1",
        (user_id, node_id),
    )
    existing = await cur.fetchone()
    if existing is None:
        raise NotFoundError(
            code="pin_not_found",
            message=f"Pin not found for node {node_id!r} by this user.",
        )

    await db.execute(
        "DELETE FROM kg_pins WHERE user_id = ? AND node_id = ?",
        (user_id, node_id),
    )
    # get_write_db() documents "caller must commit" — flush WAL immediately so
    # the deletion is visible to the read-pool without waiting for periodic flush.
    await db.commit()

    return {"deleted": True, "node_id": node_id}


async def graph_resolve(
    ctx: CallerContext,
    db: aiosqlite.Connection,
    *,
    path: str,
    visible_projects: set[str] | None,
) -> ResolveOut:
    """Resolve a file path to its graph_nodes id.

    Security (P0):
    - Rejects null bytes, absolute paths, and `..` path components with 404
      (never 403 — avoid oracle).
    - Returns 404 (not 403) on visibility miss (no oracle).

    ``visible_projects`` is resolved at the adapter boundary (DECISION 1):
    ``None`` means unrestricted (local/agent-bypass). All misses → 404 NotFoundError.
    """
    # --- Path security validation (mirrors finder._validate_path) ---
    if "\x00" in path:
        raise NotFoundError(code="not_found", message="Not found")
    if path.startswith("/"):
        raise NotFoundError(code="not_found", message="Not found")
    parts = path.replace("\\", "/").split("/")
    if ".." in parts:
        raise NotFoundError(code="not_found", message="Not found")

    # --- Lookup via file_path column (AST parser writes path here, not metadata.path) ---
    db.row_factory = aiosqlite.Row
    cur = await db.execute(
        """
        SELECT id, type, project_id
        FROM graph_nodes
        WHERE type = 'file'
          AND file_path = ?
          AND deprecated_at IS NULL
        LIMIT 1
        """,
        (path,),
    )
    row = await cur.fetchone()
    if row is None:
        raise NotFoundError(code="not_found", message="Not found")

    # --- RBAC visibility check (404, not 403) ---
    project_id = row["project_id"]
    if project_id and visible_projects is not None and project_id not in visible_projects:
        raise NotFoundError(code="not_found", message="Not found")

    return ResolveOut(node_id=row["id"], kind=row["type"])


async def graph_overview(
    ctx: CallerContext,
    db: aiosqlite.Connection,
    *,
    level: str,
    scope: str | None,
    cross_project: bool,
    limit: int,
    user,
    visible_projects: set[str] | None,
    filter_visible_edges,
) -> OverviewBundle:
    """Return graph overview at macro or module level of detail.

    ``user`` is the raw UserInfo (passed through for ``filter_visible_edges``,
    which needs ``UserInfo.teams``). ``filter_visible_edges`` is the RBAC edge
    filter callable, INJECTED by the adapter (DECISION A): it imports fastapi and
    is test-pinned to the ``routers.graph`` namespace, so the use_case never
    imports it. ``visible_projects`` is resolved at the boundary for the
    module-level RBAC check (DECISION 1). 422 on malformed inputs; 404 on a
    module visibility miss (oracle-avoidance).
    """
    if level == "module" and not scope:
        raise ValidationError(
            code="scope_required",
            message="scope is required for level=module. Format: project:artifact:<slug>",
        )

    db.row_factory = aiosqlite.Row
    hidden_count = 0

    if level == "macro":
        # Fetch project hub nodes (top-K by degree)
        cur = await db.execute(
            """
            SELECT id, type, name, qualified_name, metadata, degree
            FROM graph_nodes
            WHERE type = 'project'
              AND deprecated_at IS NULL
            ORDER BY degree DESC
            LIMIT ?
            """,
            (limit,),
        )
        node_rows = await cur.fetchall()

        nodes: list[OverviewNode] = []
        node_ids: set[str] = set()
        for r in node_rows:
            try:
                meta = json_mod.loads(r["metadata"]) if r["metadata"] else {}
            except (TypeError, ValueError):
                meta = {}
            nodes.append(OverviewNode(
                id=r["id"],
                type=r["type"],
                label=r["qualified_name"] or r["name"],
                sub_nodes=None,
                metadata=meta,
            ))
            node_ids.add(r["id"])

        # Fetch edges between these project nodes
        if node_ids:
            placeholders = ",".join("?" * len(node_ids))
            id_list = list(node_ids)
            cur = await db.execute(
                f"""
                SELECT e.source_id, e.target_id, e.relation, COUNT(*) AS weight,
                       sn.project_id AS source_project, tn.project_id AS target_project
                FROM graph_edges e
                JOIN graph_nodes sn ON sn.id = e.source_id
                JOIN graph_nodes tn ON tn.id = e.target_id
                WHERE e.source_id IN ({placeholders})
                  AND e.target_id IN ({placeholders})
                GROUP BY e.source_id, e.target_id, e.relation
                """,
                id_list + id_list,
            )
            raw_edges = [dict(r) for r in await cur.fetchall()]
        else:
            raw_edges = []

        # DECISION A: filter_visible_edges (api.visibility) imports fastapi and is
        # test-pinned to the routers.graph namespace, so it is injected by the
        # adapter — this module never imports it.
        # Apply RBAC filter
        if cross_project:
            filtered_edges, hidden_count = await filter_visible_edges(db, user, raw_edges)
        else:
            # Filter to only intra-project edges (same project_id source/target)
            filtered_edges = [
                e for e in raw_edges
                if e.get("source_project") == e.get("target_project")
            ]
            # RBAC on top
            filtered_edges, hidden_count = await filter_visible_edges(db, user, filtered_edges)

        edges = [
            OverviewEdge(
                source=e["source_id"],
                target=e["target_id"],
                relation=e["relation"],
                weight=e.get("weight", 1),
            )
            for e in filtered_edges
        ]

        return OverviewBundle(
            level="macro",
            scope=None,
            nodes=nodes,
            edges=edges,
            hidden_cross_project_count=hidden_count,
        )

    else:  # level == "module"
        # Extract project slug from scope (project:artifact:<slug>)
        parts = scope.split(":", 2) if scope else []
        if len(parts) < 3 or parts[0] != "project":
            raise ValidationError(
                code="invalid_scope",
                message=f"Invalid scope format: {scope!r}. Expected: project:artifact:<slug>",
            )
        project_slug = parts[2]

        # RBAC check for this project (404 oracle-avoidance)
        if visible_projects is not None and project_slug not in visible_projects:
            raise NotFoundError(code="not_found", message="Not found")

        # Fetch file nodes for this project, group by first path segment
        cur = await db.execute(
            """
            SELECT id, type, name, qualified_name, metadata
            FROM graph_nodes
            WHERE type = 'file'
              AND project_id = ?
              AND deprecated_at IS NULL
            ORDER BY degree DESC
            LIMIT ?
            """,
            (project_slug, limit),
        )
        file_rows = await cur.fetchall()

        # Build module nodes by grouping on first path segment
        module_map: dict[str, list] = {}
        for r in file_rows:
            try:
                meta = json_mod.loads(r["metadata"]) if r["metadata"] else {}
            except (TypeError, ValueError):
                meta = {}
            path_val = meta.get("path", r["name"] or "")
            first_seg = path_val.split("/")[0] if "/" in path_val else path_val
            if first_seg not in module_map:
                module_map[first_seg] = []
            module_map[first_seg].append(r)

        nodes = []
        for folder, file_list in module_map.items():
            node_id = f"module:artifact:{project_slug}/{folder}"
            nodes.append(OverviewNode(
                id=node_id,
                type="module",
                label=folder,
                sub_nodes=len(file_list),
                metadata={"folder": folder, "project": project_slug},
            ))

        return OverviewBundle(
            level="module",
            scope=scope,
            nodes=nodes,
            edges=[],  # module-level edges are computed on demand (P3/P4)
            hidden_cross_project_count=0,
        )


async def graph_orphans(
    ctx: CallerContext,
    db: aiosqlite.Connection,
    *,
    scope: str,
    visible_projects: set[str] | None,
) -> OrphansBundle:
    """Return file nodes with no edges (orphans) within a scope.

    Uses a LEFT JOIN anti-join so the planner can use the source/target indexes.
    Files grouped by first path segment, capped at 30 per sub-cluster.

    ``visible_projects`` resolved at the boundary (DECISION 1); 404 on miss
    (oracle-avoidance). 422 on malformed scope (defence-in-depth; the router's
    Query pattern also gates it).
    """
    # Extract project_id from scope
    parts = scope.split(":", 2)
    if len(parts) < 3:
        raise ValidationError(code="invalid_scope", message=f"Invalid scope: {scope!r}")

    project_slug = parts[2].split("/")[0] if "/" in parts[2] else parts[2]

    # RBAC: check project visibility (404 oracle-avoidance)
    if visible_projects is not None and project_slug not in visible_projects:
        raise NotFoundError(code="not_found", message="Not found")

    db.row_factory = aiosqlite.Row

    # LEFT JOIN anti-join query — must use indexed columns source_id / target_id
    cur = await db.execute(
        """
        SELECT n.id, n.name, n.qualified_name, n.metadata, n.updated_at
        FROM graph_nodes n
        LEFT JOIN graph_edges e1 ON e1.source_id = n.id
        LEFT JOIN graph_edges e2 ON e2.target_id = n.id
        WHERE n.type = 'file'
          AND e1.source_id IS NULL
          AND e2.target_id IS NULL
          AND n.deprecated_at IS NULL
          AND n.project_id = ?
        ORDER BY n.name
        """,
        (project_slug,),
    )
    rows = await cur.fetchall()

    # Group by first path segment
    cluster_map: dict[str, list[dict]] = {}
    for r in rows:
        try:
            meta = json_mod.loads(r["metadata"]) if r["metadata"] else {}
        except (TypeError, ValueError):
            meta = {}
        path_val = meta.get("path", r["name"] or "")
        folder = path_val.split("/")[0] if "/" in path_val else "(root)"
        if folder not in cluster_map:
            cluster_map[folder] = []
        cluster_map[folder].append({
            "node_id": r["id"],
            "label": r["qualified_name"] or r["name"],
            "path": path_val,
            "last_modified": r["updated_at"],
        })

    # Build sub-clusters with 30-cap
    sub_clusters: list[OrphanSubCluster] = []
    for folder, file_list in sorted(cluster_map.items()):
        total = len(file_list)
        capped = file_list[:30]
        overflow = total - len(capped)
        sub_clusters.append(OrphanSubCluster(
            folder=folder,
            color=orphan_color(folder),
            count=total,
            files=[
                OrphanFile(
                    node_id=f["node_id"],
                    label=f["label"],
                    path=f["path"],
                    last_modified=parse_db_datetime(f["last_modified"]) if f["last_modified"] else None,
                )
                for f in capped
            ],
            overflow_count=overflow,
        ))

    return OrphansBundle(scope=scope, sub_clusters=sub_clusters)


# ---------------------------------------------------------------------------
# Fase D — KG trust write-path (the "button"). Behind MARVIS_TEMPORAL_MEMORY.
# These are the apply actions an agent performs AFTER Triage approves a Brain
# proposal — guidance-only is preserved (the Brain cycle never calls these).
# Writes go through the single-writer API connection (Constitution Rule 2:
# no raw sqlite to prod). Soft, never deletes.
# ---------------------------------------------------------------------------

def _iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%f") + "Z"


def _require_temporal_memory() -> None:
    """Hard-gate the trust writes on the flag → inert (ship-dark) when off."""
    from core.api.config import settings
    if not settings.temporal_memory_enabled:
        raise ValidationError(
            code="temporal_memory_disabled",
            message=(
                "KG trust writes require MARVIS_TEMPORAL_MEMORY=on. The flag is "
                "off → this mutation is inert by design. Fix: enable the flag in "
                "the API environment, or do not call this tool."
            ),
        )


async def mark_node_verified(
    ctx: CallerContext,
    db: aiosqlite.Connection,
    *,
    node_id: str,
    verified_at: str | None = None,
) -> dict:
    """Stamp graph_nodes.last_verified_at = now (REINFORCE apply).

    Flag-gated. 404 if the node is missing/soft-deleted. Idempotent: re-running
    just refreshes the timestamp. The read-time needs_review (Fase D read-wiring)
    immediately treats the re-verified node as fresh.
    """
    _require_temporal_memory()
    try:
        graph_service.validate_node_id(node_id)
    except ValueError as e:
        raise ValidationError(code="invalid_request", message=str(e))
    db.row_factory = aiosqlite.Row
    cur = await db.execute(
        "SELECT id FROM graph_nodes WHERE id = ? AND deprecated_at IS NULL LIMIT 1",
        (node_id,),
    )
    if await cur.fetchone() is None:
        raise NotFoundError(
            code="node_not_found",
            message=(
                f"Node not found: {node_id!r}. Either it does not exist or is "
                "soft-deleted. Fix: verify node_id format and that it is live."
            ),
        )
    now = verified_at or _iso_now()
    await db.execute(
        "UPDATE graph_nodes SET last_verified_at = ? WHERE id = ?", (now, node_id)
    )
    await db.commit()
    return {"node_id": node_id, "last_verified_at": now, "action": "verified"}


async def mark_edge_superseded(
    ctx: CallerContext,
    db: aiosqlite.Connection,
    *,
    source_id: str,
    target_id: str,
    relation: str,
    superseded_by: str | None = None,
    invalid_at: str | None = None,
) -> dict:
    """Soft-invalidate an asserted edge: set valid_until = now (+ optional
    superseded_by audit pointer). SUPERSEDE / CONTRADICTION apply.

    Flag-gated. Soft, never deletes — the row stays; valid_until excludes it from
    default reads via the existing temporal filter. 404 if the triple is absent.
    """
    _require_temporal_memory()
    try:
        graph_service.validate_node_id(source_id)
        graph_service.validate_node_id(target_id)
    except ValueError as e:
        raise ValidationError(code="invalid_request", message=str(e))
    if relation not in graph_service.EDGE_TYPES:
        raise ValidationError(
            code="invalid_request",
            message=(
                f"Invalid relation: {relation!r}. "
                f"Allowed: {list(graph_service.EDGE_TYPES)}."
            ),
        )
    db.row_factory = aiosqlite.Row
    cur = await db.execute(
        "SELECT id FROM graph_edges "
        "WHERE source_id = ? AND target_id = ? AND relation = ? LIMIT 1",
        (source_id, target_id, relation),
    )
    row = await cur.fetchone()
    if row is None:
        raise NotFoundError(
            code="edge_not_found",
            message=(
                f"Edge not found: ({source_id!r})-[{relation}]->({target_id!r}). "
                "Fix: verify the triple exists in graph_edges."
            ),
        )
    now = invalid_at or _iso_now()
    await db.execute(
        "UPDATE graph_edges SET valid_until = ?, superseded_by = ? WHERE id = ?",
        (now, superseded_by, row["id"]),
    )
    await db.commit()
    return {
        "source_id": source_id,
        "target_id": target_id,
        "relation": relation,
        "valid_until": now,
        "superseded_by": superseded_by,
        "action": "superseded",
    }
