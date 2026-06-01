# v2.0.0 - 2026-05-27 - S1 F1.8: thin adapter over use_cases.graph (visibility + rate-limit + share kept at adapter)
# v1.8.0 - 2026-04-17 - P2: 5 new UX endpoints (landing/pins/resolve/overview/orphans) + slowapi + filter_visible_edges
# v1.7.0 - 2026-04-16 - H2: graph ACL — get_visible_projects check on ?project=X endpoints
# v1.5.0 - 2026-04-14 - KG Fase 2: cross-project (project filter + edge_types + BFS cycle detection)
# v1.4.0 - 2026-04-14 - KG Fase 1f: impact + context + pattern endpoints
"""HTTP adapter for the Knowledge Graph domain (S1 collapse-runtime).

This router is a thin transport adapter. All query orchestration + validation +
error mapping lives in :mod:`core.api.use_cases.graph` (pure, fastapi-free). Each
handler resolves identity into a :class:`CallerContext`, calls the use_case inside
``try/except`` -> ``to_http_legacy``, and owns the transport concerns.

Endpoints:
  GET /api/v1/graph/neighbors/{node_id}                — live view (Fase 1a)
  GET /api/v1/graph/neighbors/{node_id}?rank=X         — ranked (Fase 1b)
  GET /api/v1/graph/neighbors/{node_id}?as_of=<iso>    — time-travel (Fase 1d)
  GET /api/v1/graph/neighbors/{node_id}?project=X&edge_types=Y&edge_types=Z  — cross-project (Fase 2)
  GET /api/v1/graph/hotspots?window=30d&limit=20       — churn hotspots (Fase 1e)
  GET /api/v1/graph/impact/{node_id}?depth=2&limit=50  — reverse impact (Fase 1f)
  GET /api/v1/graph/impact/{id}?project=X&edge_types=mentions — multi-hop cross-project (Fase 2)
  GET /api/v1/graph/context/{node_id}                  — rationale chain (Fase 1f)
  GET /api/v1/graph/pattern?scope=<path|module>        — applicable learnings (Fase 1f)
  GET /api/v1/graph/function-share/{qualified_name}    — share + KG context bundle (vision audit P1)
  --- P2 UX endpoints ---
  GET  /api/v1/graph/landing                           — hotspots + recent + pins bundle
  GET  /api/v1/graph/pins                              — list user pins (user-scoped)
  POST /api/v1/graph/pins                              — upsert pin (Bearer required, CSRF via Bearer)
  DELETE /api/v1/graph/pins/{node_id}                  — delete pin
  GET  /api/v1/graph/resolve?path=...                  — resolve file path to node_id
  GET  /api/v1/graph/overview?level=macro|module       — LOD overview graph
  GET  /api/v1/graph/orphans?scope=...                 — orphan files (LEFT JOIN anti-join)
  GET  /api/v1/graph/cosmo                             — Cosmo canvas dataset

Read-only — uses get_db (read-only pool). Auth via the standard dual
cookie+Bearer dependency `get_current_user_or_agent`.

CSRF on pin writes: POST/DELETE /graph/pins require Authorization: Bearer <token>.
The cookie-based session is not accepted for mutations (Bearer-required approach).

STAYS IN THE ADAPTER (transport concerns):
  * VISIBILITY enforcement (use_cases.graph DECISION B): ``get_visible_projects``
    is resolved here AND the gate fires here, because the per-endpoint outcome is
    test-pinned to a plain-string body — project-scoped reads → 403
    "Project not accessible" (test_kg_security_h2_h3), oracle reads → 404
    "Not found". For resolve/overview/orphans the resolved ``visible_projects`` is
    PASSED INTO the use_case (DECISION 1), which raises NotFoundError on a miss.
    ``filter_visible_edges`` (overview macro RBAC) is imported here and INJECTED
    into the use_case: it imports fastapi and the overview test pins it to THIS
    namespace (``patch("api.routers.graph.filter_visible_edges")``).
  * RATE LIMITS (@limiter.limit) — slowapi decorators stay on the handlers.
  * SHARE-URL side effect for ``function-share`` (use_cases.graph DECISION C):
    role enforcement + signed URL via ``share_links`` (imports fastapi) + the
    on-disk preview read stay here; the pure KG-context pieces live in the use_case.
  * PIN-WRITE TTLCache invalidation (the caches are module-level transport state).

ERROR BOUNDARY: graph's HTTPException detail bodies are rich plain strings (part
of the agent-facing contract, NOT the structured ``{code,message}`` shape). The
use_case raises :class:`ServiceError` whose ``message`` is byte-identical to the
legacy detail; ``to_http_legacy`` re-raises ``HTTPException(status, message)`` so
bodies are unchanged.
"""
from __future__ import annotations

import logging
import re
from typing import Literal

import aiosqlite
from cachetools import TTLCache
from fastapi import APIRouter, Depends, HTTPException, Query, Request

from core.api.db import get_db, get_write_db
from core.api.models import UserInfo
from core.api.models.graph import GraphCapabilities, RankType
from core.api.models.graph_ux import (
    LandingBundle,
    OrphansBundle,
    OverviewBundle,
    PinIn,
    PinOut,
    ResolveOut,
)
from core.api.models.graph_cosmo import GraphCosmoOut
from core.api.rate_limit import limiter
from core.api.security import get_current_user_or_agent
from core.api.services import graph_cosmo_service
from core.api.use_cases import graph as uc
from core.api.use_cases._context import CallerContext
from core.api.use_cases._errors import ServiceError
from core.api.visibility import filter_visible_edges, get_visible_projects
from core.api.services.share_links import (
    create_shared_link_record,
    enforce_workspace_share_role,
    public_repo_path,
    stored_repo_path,
    validate_repo_path,
)

logger = logging.getLogger(__name__)


def to_http_legacy(err: ServiceError) -> HTTPException:
    """Map a domain ``ServiceError`` to an ``HTTPException`` with the LEGACY body.

    Graph endpoints have always returned a plain-string ``detail`` (the rich,
    agent-facing message). The structured ``{code,message}`` body produced by
    ``routers/_adapter.to_http`` would change every response and break the
    string-substring assertions (test_kg_security_h2_h3, test_kg_share_function).
    So we preserve the plain-string body here.
    """
    return HTTPException(status_code=err.http_status, detail=err.message)


# Relation types backed by the latest graph_edges CHECK constraint (mig 132).
# Kept here (not imported from graph_service) because FastAPI Literal typing
# requires a literal value set for static introspection. Keep synchronized
# with graph_service.EDGE_TYPES + mcp-pir edgeTypeEnum + docs kg-fetcher.ts +
# kg-schema-snapshot.json + latest migration CHECK (see scripts/_drift_check.py
# check B for the 6-source sync mandate).
EdgeType = Literal[
    "calls", "imports", "defines",
    "produces", "contains",
    "describes", "documents", "cites", "applies_to",
    "depends_on", "mentions", "refers_to", "shares_tag", "similar_to",
    "resolves_to",  # Phase 7.2: module stub -> file canonical bridge
    "modifies",  # KG PR-Impact (mig 132): pr_artifact -> function_artifact
]

PROJECT_SLUG_QUERY_PATTERN = r"^[a-z0-9][a-z0-9&\-]+$"

router = APIRouter(prefix="/api/v1/graph", tags=["graph"])


@router.get("/neighbors/{node_id}")
async def graph_neighbors(
    node_id: str,
    relation: str | None = Query(
        None, description="Deprecated (Fase 1). Prefer `edge_types`."
    ),
    edge_types: list[EdgeType] | None = Query(
        None,
        description="Fase 2: repeatable filter on edge relations. "
        "Usage: ?edge_types=calls&edge_types=depends_on — union semantics. "
        "Overrides `relation` when both are set.",
    ),
    project: str | None = Query(
        None,
        max_length=50,
        pattern=PROJECT_SLUG_QUERY_PATTERN,
        description="Fase 2: filter neighbours by project_id (ARCH-01 "
        "project_scope=source). Pass slug like 'marvisx' or 'c&i-normativa'.",
    ),
    direction: Literal["incoming", "outgoing", "both"] = Query(
        "both",
        description="incoming = who points to this; outgoing = what this points to",
    ),
    limit: int = Query(50, ge=1, le=200),
    rank: RankType = Query(
        "none",
        description="Ranker strategy: none (raw) or suspect_write (Fase 1b)",
    ),
    as_of: str | None = Query(
        None,
        description="ISO timestamp for time-travel query (YYYY-MM-DD[ HH:MM:SS]). "
        "When set, returns the graph state as it was at that moment. "
        "When omitted, returns the live view (excludes deprecated nodes/edges).",
        max_length=32,
        pattern=r"^\d{4}-\d{2}-\d{2}([ T]\d{2}:\d{2}:\d{2}(\.\d+)?)?Z?$",
    ),
    user: UserInfo = Depends(get_current_user_or_agent),
    db: aiosqlite.Connection = Depends(get_db),
) -> dict:
    """Return neighbours of a graph node, optionally ranked / time-travelled / cross-project.

    422 on malformed `node_id`, `relation`, `edge_types`, `project`, `rank`, or `as_of`.
    404 if the node does not exist in the graph (at `as_of` if specified).
    """
    # H2: visibility check — only when caller scopes to a specific project
    if project:
        visible = await get_visible_projects(db, user)
        if visible is not None and project not in visible:
            raise HTTPException(status_code=403, detail="Project not accessible")

    ctx = CallerContext.from_user_info(user, is_human_session=False)
    try:
        return await uc.graph_neighbors(
            ctx, db,
            node_id=node_id, relation=relation, edge_types=edge_types,
            project=project, direction=direction, limit=limit, rank=rank, as_of=as_of,
        )
    except ServiceError as e:
        raise to_http_legacy(e)


@router.get("/hotspots")
async def graph_hotspots(
    window: Literal["7d", "30d", "total"] = Query(
        "30d",
        description="Rolling window: 7d/30d for recent churn, total for all-time.",
    ),
    limit: int = Query(20, ge=1, le=100),
    type_filter: Literal["function", "file", "all"] = Query(
        "file",
        description="Filter by node type (file = default, all = function+file).",
    ),
    project: str | None = Query(
        None,
        max_length=50,
        pattern=PROJECT_SLUG_QUERY_PATTERN,
        description="Fase 2: filter hotspots to this project (default: all projects).",
    ),
    user: UserInfo = Depends(get_current_user_or_agent),
    db: aiosqlite.Connection = Depends(get_db),
) -> dict:
    """Top N hotspot nodes ordered by touch count in the chosen window.

    422 on malformed `window`, `type_filter`, or `project`.
    """
    # H2: visibility check — only when caller scopes to a specific project
    if project:
        visible = await get_visible_projects(db, user)
        if visible is not None and project not in visible:
            raise HTTPException(status_code=403, detail="Project not accessible")

    ctx = CallerContext.from_user_info(user, is_human_session=False)
    try:
        return await uc.graph_hotspots(
            ctx, db,
            window=window, limit=limit, type_filter=type_filter, project=project,
        )
    except ServiceError as e:
        raise to_http_legacy(e)


# ---------------------------------------------------------------------------
# Fase 1f — graph_impact / graph_context / graph_pattern
# ---------------------------------------------------------------------------


@router.get("/impact/{node_id}")
async def graph_impact_endpoint(
    node_id: str,
    depth: int = Query(
        2, ge=1, le=5,
        description="BFS hops for transitive caller discovery (1 = direct only). "
        "Fase 2 caps at min(requested, 4) for multi-hop safety.",
    ),
    limit: int = Query(
        50, ge=1, le=200,
        description="Max transitive callers returned (hard-capped at 200).",
    ),
    edge_types: list[EdgeType] | None = Query(
        None,
        description="Fase 2: repeatable filter on edge relations. "
        "Default (None) walks along `calls` edges. Pass e.g. "
        "?edge_types=calls&edge_types=depends_on&edge_types=mentions for "
        "cross-project multi-hop impact.",
    ),
    project: str | None = Query(
        None,
        max_length=50,
        pattern=PROJECT_SLUG_QUERY_PATTERN,
        description="Fase 2: scope walk to source nodes belonging to this project.",
    ),
    user: UserInfo = Depends(get_current_user_or_agent),
    db: aiosqlite.Connection = Depends(get_db),
) -> dict:
    """Reverse impact analysis for a graph node.

    422 on malformed `node_id`, `edge_types`, or `project`.
    404 if the node does not exist.
    """
    # H2: visibility check — only when caller scopes to a specific project
    if project:
        visible = await get_visible_projects(db, user)
        if visible is not None and project not in visible:
            raise HTTPException(status_code=403, detail="Project not accessible")

    ctx = CallerContext.from_user_info(user, is_human_session=False)
    try:
        return await uc.graph_impact(
            ctx, db,
            node_id=node_id, depth=depth, limit=limit,
            edge_types=edge_types, project=project,
        )
    except ServiceError as e:
        raise to_http_legacy(e)


@router.get("/context/{node_id}")
async def graph_context_endpoint(
    node_id: str,
    per_category_limit: int = Query(
        5, ge=1, le=20,
        description="Max items per category (commits/prs/tasks/handoffs/learnings).",
    ),
    project: str | None = Query(
        None,
        max_length=50,
        pattern=PROJECT_SLUG_QUERY_PATTERN,
        description="Fase 2: scope source node to this project (ARCH-01). "
        "The chain nodes can still cross projects for multi-hop discovery.",
    ),
    user: UserInfo = Depends(get_current_user_or_agent),
    db: aiosqlite.Connection = Depends(get_db),
) -> dict:
    """Trace the rationale chain for a node.

    422 on malformed `node_id` or `project`.
    404 if the node does not exist.
    """
    # H2: visibility check — only when caller scopes to a specific project
    if project:
        visible = await get_visible_projects(db, user)
        if visible is not None and project not in visible:
            raise HTTPException(status_code=403, detail="Project not accessible")

    ctx = CallerContext.from_user_info(user, is_human_session=False)
    try:
        return await uc.graph_context(
            ctx, db,
            node_id=node_id, per_category_limit=per_category_limit, project=project,
        )
    except ServiceError as e:
        raise to_http_legacy(e)


@router.get("/pattern")
async def graph_pattern_endpoint(
    scope: str = Query(
        ..., max_length=256,
        description="File path (api/db.py), module dotted name (api.db), or full node id.",
    ),
    limit: int = Query(
        20, ge=1, le=100,
        description="Max learnings returned.",
    ),
    project: str | None = Query(
        None,
        max_length=50,
        pattern=PROJECT_SLUG_QUERY_PATTERN,
        description="Fase 2: filter learnings to those whose `applies_to` target "
        "lives in this project.",
    ),
    user: UserInfo = Depends(get_current_user_or_agent),
    db: aiosqlite.Connection = Depends(get_db),
) -> dict:
    """Learnings applicable to a scope (file, module, or specific function).

    422 on malformed `scope` or `project`.
    """
    # H2: visibility check — only when caller scopes to a specific project
    if project:
        visible = await get_visible_projects(db, user)
        if visible is not None and project not in visible:
            raise HTTPException(status_code=403, detail="Project not accessible")

    ctx = CallerContext.from_user_info(user, is_human_session=False)
    try:
        return await uc.graph_pattern(
            ctx, db, scope=scope, limit=limit, project=project,
        )
    except ServiceError as e:
        raise to_http_legacy(e)


# ---------------------------------------------------------------------------
# capabilities — KG schema metadata for agent discovery (Phase 7.x Pilastro 5)
# ---------------------------------------------------------------------------


@router.get(
    "/capabilities",
    response_model=GraphCapabilities,
    summary="KG schema metadata for agent discovery",
)
async def get_capabilities(
    user: UserInfo = Depends(get_current_user_or_agent),
    db: aiosqlite.Connection = Depends(get_db),
) -> GraphCapabilities:
    """Returns valid edge_types, node_kinds, node_prefixes + schema_version.

    Agents should call this on cold-start (before building graph_* queries)
    to discover the current taxonomy instead of hardcoding enums.

    Security (deepen section 9): mcp_version NOT exposed (no fingerprinting).
    """
    ctx = CallerContext.from_user_info(user, is_human_session=False)
    try:
        return await uc.get_capabilities(ctx, db)
    except ServiceError as e:
        raise to_http_legacy(e)


# ---------------------------------------------------------------------------
# share_function — daily-QoL tool combining share_file + KG context bundle
# ---------------------------------------------------------------------------

SHARE_FUNCTION_DEFAULT_HOURS: int = uc.SHARE_FUNCTION_DEFAULT_HOURS
PREVIEW_LINES: int = uc.PREVIEW_LINES


def _read_file_preview(
    file_path: str, line_number: int | None, n_lines: int = PREVIEW_LINES
) -> dict:
    """Read `n_lines` from `file_path` starting at `line_number` (1-based).

    Transport concern: ``validate_repo_path`` (share_links) imports fastapi, and
    reading off disk is an I/O side effect — kept in the adapter. Returns
    `{start_line, end_line, lines: [str]}` or `{error: ...}` so the rest of the
    bundle survives.
    """
    try:
        target = validate_repo_path(file_path)
    except HTTPException as exc:
        return {"error": f"path validation failed: {exc.detail}"}
    if not target.is_file():
        return {"error": "file not found on disk"}
    try:
        content = target.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return {"error": f"read failed: {exc}"}

    all_lines = content.splitlines()
    total = len(all_lines)
    start = max(1, int(line_number or 1))
    # Clamp start to file length to avoid empty preview on stale line_number.
    if start > total:
        start = max(1, total - n_lines + 1)
    end = min(total, start + n_lines - 1)
    # Slice uses 0-based indexing.
    lines = all_lines[start - 1 : end]
    return {
        "start_line": start,
        "end_line": end,
        "total_lines": total,
        "lines": lines,
    }


@router.get("/function-share/{qualified_name:path}")
async def share_function_endpoint(
    qualified_name: str,
    include: str | None = Query(
        None,
        max_length=128,
        description="CSV list of blocks to include. Allowed: "
        "preview,neighbors,context,hotspot. Omit or leave empty for all four.",
    ),
    hours: int = Query(
        SHARE_FUNCTION_DEFAULT_HOURS,
        ge=1, le=720,
        description="Hours until the share URL expires (1-720, default 24).",
    ),
    current_user: UserInfo = Depends(get_current_user_or_agent),
    db: aiosqlite.Connection = Depends(get_write_db),
) -> dict:
    """Hand off a function with full KG context in a single call.

    Given a Python `qualified_name` (e.g. `api.db.get_db`), returns share_url +
    preview + neighbors + context + hotspot. See use_cases.graph DECISION C for
    the transport/pure split.

    422 on malformed `qualified_name` or unknown `include` tokens.
    404 if no `type='function'` node matches `qualified_name` in the live graph.
    """
    ctx = CallerContext.from_user_info(current_user, is_human_session=False)

    # 1. Validate qualified_name + includes (pure → use_case, 422 via ServiceError).
    try:
        uc.validate_qualified_name(qualified_name)
        include_list = uc.parse_include_csv(include)
    except ValueError as e:
        # parse_include_csv raises ValueError (pure helper) — map to 422.
        raise HTTPException(status_code=422, detail=str(e))
    except ServiceError as e:
        raise to_http_legacy(e)

    # 2. Enforce workspace-share role (transport — share_links imports fastapi).
    enforce_workspace_share_role(current_user)

    # 3. Lookup the function node (pure → use_case, 404 via ServiceError).
    try:
        node_row = await uc.lookup_function_node(db, qualified_name=qualified_name)
    except ServiceError as e:
        raise to_http_legacy(e)

    node_id = node_row["id"]
    file_path = node_row["file_path"]
    line_number = node_row["line_number"]

    # 4. Generate the share URL (transport side effect — primary pull-factor).
    #    Pattern mirrors finder._create_share_link_impl workspace branch.
    try:
        target = validate_repo_path(file_path)
    except HTTPException as exc:
        raise HTTPException(
            status_code=404,
            detail=(
                f"File path for {qualified_name!r} rejected by validate_repo_path: {exc.detail}. "
                f"Recorded file_path in graph_nodes: {file_path!r}. "
                "Reason: the indexed path resolves outside the allowed share root, or contains traversal tokens. "
                "Fix: re-run the indexer with the correct repo_share_root, or fix the graph_nodes row manually."
            ),
        ) from exc
    if not target.is_file():
        raise HTTPException(
            status_code=404,
            detail=(
                f"File {file_path!r} no longer exists on disk. "
                "Reason: the graph_nodes row is stale — the file was moved or deleted since indexing. "
                "Fix: re-run scripts/populate_knowledge_graph.py to refresh, "
                "or locate the new path with `git log --diff-filter=D --name-only` (deleted) or mcp__pir__search."
            ),
        )

    share_record = await create_shared_link_record(
        stored_path=stored_repo_path(file_path),
        public_path=public_repo_path(file_path),
        current_user=current_user,
        db=db,
        hours=hours,
    )

    response: dict = {
        "qualified_name": qualified_name,
        "node_id": node_id,
        "file_path": file_path,
        "line_number": line_number,
        "share_url": share_record["url"],
        "share_token": share_record["token"],
        "share_expires_at": share_record["expires_at"],
        "includes": include_list,
    }

    # 5. Preview block (transport — disk read kept in the adapter).
    if "preview" in include_list:
        response["preview"] = _read_file_preview(
            file_path=file_path, line_number=line_number,
        )

    # 6. Pure KG-context blocks (neighbors / context / hotspot → use_case).
    try:
        response.update(
            await uc.build_share_function_blocks(
                db, node_id=node_id, node_row=node_row, include_list=include_list,
            )
        )
    except ServiceError as e:
        raise to_http_legacy(e)

    return response


# ---------------------------------------------------------------------------
# P2 UX endpoints — landing / pins / resolve / overview / orphans
# ---------------------------------------------------------------------------

# Split TTL caches (transport state — kept in the adapter):
#   hotspots_cache[workspace_id]  — 60s TTL, expensive query
#   recent_cache[workspace_id]    — 60s TTL, medium query
#   pins_cache[user_id]           — 60s TTL, cheap query, invalidated on write
_hotspots_cache: TTLCache = TTLCache(maxsize=128, ttl=60)
_recent_cache: TTLCache = TTLCache(maxsize=128, ttl=60)
_pins_cache: TTLCache = TTLCache(maxsize=512, ttl=60)


# --- Landing ---

@router.get("/landing", response_model=LandingBundle, summary="KG landing bundle")
@limiter.limit("60/minute")
async def graph_landing(
    request: Request,
    user: UserInfo = Depends(get_current_user_or_agent),
    db: aiosqlite.Connection = Depends(get_db),
) -> LandingBundle:
    """Aggregated landing bundle: top-10 hotspots (30d) + last-20 recent artifacts + saved pins.

    Uses split TTLCache (60s) keyed by workspace_id for hotspots/recent, and
    user_id for pins. All three slices are merged at request time.

    Pins on soft-deleted nodes are excluded (JOIN with graph_nodes WHERE deprecated_at IS NULL).

    Rate limit: 60/minute/user.
    """
    ws_id = getattr(user, "workspace_id", "ws_default") or "ws_default"
    user_id = getattr(user, "user_id", "unknown")

    hotspots_key = f"hotspots:{ws_id}"
    recent_key = f"recent:{ws_id}"
    pins_key = f"pins:{user_id}"

    ctx = CallerContext.from_user_info(user, is_human_session=False)
    try:
        bundle, hotspots, recent, saved = await uc.graph_landing(
            ctx, db,
            workspace_id=ws_id, user_id=user_id,
            hotspots_cached=_hotspots_cache.get(hotspots_key),
            recent_cached=_recent_cache.get(recent_key),
            pins_cached=_pins_cache.get(pins_key),
        )
    except ServiceError as e:
        raise to_http_legacy(e)

    # Repopulate caches with the (possibly freshly-computed) slices.
    _hotspots_cache[hotspots_key] = hotspots
    _recent_cache[recent_key] = recent
    _pins_cache[pins_key] = saved
    return bundle


# --- List pins ---

@router.get("/pins", response_model=list[PinOut], summary="List user KG pins")
@limiter.limit("120/minute")
async def list_graph_pins(
    request: Request,
    user: UserInfo = Depends(get_current_user_or_agent),
    db: aiosqlite.Connection = Depends(get_db),
) -> list[PinOut]:
    """List all pins for the current user, ordered by pinned_at DESC.

    Pins on soft-deleted nodes are excluded (JOIN graph_nodes WHERE deprecated_at IS NULL).
    Rate limit: 120/minute/user.
    """
    user_id = getattr(user, "user_id", "unknown")
    ctx = CallerContext.from_user_info(user, is_human_session=False)
    try:
        return await uc.list_graph_pins(ctx, db, user_id=user_id)
    except ServiceError as e:
        raise to_http_legacy(e)


# --- Create/upsert pin ---
# CSRF mitigation: Bearer-required (get_current_user_or_agent accepts Bearer).
# POST with Authorization header triggers CORS preflight for cross-origin requests,
# blocking CSRF. Cookie-only sessions are still accepted for same-origin Console.

@router.post("/pins", response_model=PinOut, summary="Pin a KG node (upsert)")
@limiter.limit("30/minute")
async def create_graph_pin(
    body: PinIn,
    request: Request,
    user: UserInfo = Depends(get_current_user_or_agent),
    db: aiosqlite.Connection = Depends(get_write_db),
) -> PinOut:
    """Upsert a node pin for the current user.

    Idempotent: POST twice with the same node_id → 200, single row.
    Updates `note` on conflict. Returns 200 (not 201) to signal idempotency.

    CSRF: requires Authorization: Bearer header (or valid session cookie for same-origin).
    Rate limit: 30/minute/user.

    Invalidates pins_cache[user_id] for landing bundle consistency.
    """
    user_id = getattr(user, "user_id", "unknown")
    ws_id = getattr(user, "workspace_id", "ws_default") or "ws_default"

    ctx = CallerContext.from_user_info(user, is_human_session=False)
    try:
        result = await uc.create_graph_pin(
            ctx, db,
            workspace_id=ws_id, user_id=user_id, node_id=body.node_id, note=body.note,
        )
    except ServiceError as e:
        raise to_http_legacy(e)

    # Invalidate pins_cache for this user (transport).
    _pins_cache.pop(f"pins:{user_id}", None)
    return result


# --- Delete pin ---

@router.delete("/pins/{node_id}", response_model=dict, summary="Unpin a KG node")
@limiter.limit("30/minute")
async def delete_graph_pin(
    node_id: str,
    request: Request,
    user: UserInfo = Depends(get_current_user_or_agent),
    db: aiosqlite.Connection = Depends(get_write_db),
) -> dict:
    """Remove a pin for the current user.

    Returns 200 on success, 404 if the pin does not exist for this user.
    CSRF: same policy as POST /graph/pins (Bearer-required mitigates CSRF).
    Rate limit: 30/minute/user.

    Invalidates pins_cache[user_id] for landing bundle consistency.
    """
    user_id = getattr(user, "user_id", "unknown")
    ctx = CallerContext.from_user_info(user, is_human_session=False)
    try:
        result = await uc.delete_graph_pin(ctx, db, user_id=user_id, node_id=node_id)
    except ServiceError as e:
        raise to_http_legacy(e)

    # Invalidate pins_cache for this user (transport).
    _pins_cache.pop(f"pins:{user_id}", None)
    return result


# --- Resolve file path → node_id ---

@router.get("/resolve", response_model=ResolveOut, summary="Resolve file path to KG node")
@limiter.limit("120/minute")
async def graph_resolve(
    request: Request,
    path: str = Query(..., max_length=1024, description="Relative file path (no .. or absolute)"),
    user: UserInfo = Depends(get_current_user_or_agent),
    db: aiosqlite.Connection = Depends(get_db),
) -> ResolveOut:
    """Resolve a file path to its graph_nodes id.

    Security (P0): rejects null bytes / absolute / `..` with 404 (never 403 —
    no oracle). Returns 404 (not 403) on a visibility miss.

    Rate limit: 120/minute/user.
    """
    # DECISION 1: resolve visibility at the boundary; the use_case enforces (404).
    visible = await get_visible_projects(db, user)
    ctx = CallerContext.from_user_info(user, is_human_session=False)
    try:
        return await uc.graph_resolve(ctx, db, path=path, visible_projects=visible)
    except ServiceError as e:
        raise to_http_legacy(e)


# --- Overview (macro / module LOD) ---

@router.get("/overview", response_model=OverviewBundle, summary="KG overview (macro/module LOD)")
@limiter.limit("6/minute")
async def graph_overview(
    request: Request,
    level: Literal["macro", "module"] = Query(
        ..., description="Detail level: macro = project nodes, module = module nodes"
    ),
    scope: str | None = Query(
        None,
        max_length=256,
        description="Required for level=module. Format: project:artifact:<slug>",
    ),
    cross_project: bool = Query(
        True,
        description="Include cross-project edges (default: True, D14)",
    ),
    limit: int = Query(300, ge=1, le=300),
    user: UserInfo = Depends(get_current_user_or_agent),
    db: aiosqlite.Connection = Depends(get_db),
) -> OverviewBundle:
    """Return graph overview at macro or module level of detail.

    macro: project:artifact:* nodes + aggregated cross-project edges, capped by degree.
    module: module nodes for a given project scope.

    RBAC: filter_visible_edges applied (macro) — hidden_cross_project_count returned;
    module-level visibility miss → 404.
    Rate limit: 6/minute/user (expensive).
    """
    # DECISION 1: resolve visibility at the boundary for the module-level RBAC
    # check; the macro path applies filter_visible_edges inside the use_case
    # (needs the full UserInfo, passed through). filter_visible_edges is INJECTED
    # so the overview test can patch it at this namespace (DECISION A).
    visible = await get_visible_projects(db, user)
    ctx = CallerContext.from_user_info(user, is_human_session=False)
    try:
        return await uc.graph_overview(
            ctx, db,
            level=level, scope=scope, cross_project=cross_project, limit=limit,
            user=user, visible_projects=visible,
            filter_visible_edges=filter_visible_edges,
        )
    except ServiceError as e:
        raise to_http_legacy(e)


# --- Orphans ---

_ORPHAN_SCOPE_PATTERN = r"^(project|module):artifact:.+$"

@router.get("/orphans", response_model=OrphansBundle, summary="KG orphan files (unlinked)")
@limiter.limit("30/minute")
async def graph_orphans(
    request: Request,
    scope: str = Query(
        ...,
        max_length=256,
        pattern=_ORPHAN_SCOPE_PATTERN,
        description="Scope to search for orphans: project:artifact:<slug> or module:artifact:<project>/<folder>",
    ),
    user: UserInfo = Depends(get_current_user_or_agent),
    db: aiosqlite.Connection = Depends(get_db),
) -> OrphansBundle:
    """Return file nodes with no edges (orphans) within a scope.

    Uses a LEFT JOIN anti-join — NOT a NOT IN subquery — so the query planner
    can use idx_graph_edges_source_id and idx_graph_edges_target_id.

    Files grouped by first path segment, capped at 30 per sub-cluster.
    Rate limit: 30/minute/user.
    """
    # DECISION 1: resolve visibility at the boundary; the use_case enforces (404).
    visible = await get_visible_projects(db, user)
    ctx = CallerContext.from_user_info(user, is_human_session=False)
    try:
        return await uc.graph_orphans(ctx, db, scope=scope, visible_projects=visible)
    except ServiceError as e:
        raise to_http_legacy(e)


# --- Cosmo canvas adapter (PR #3) ---


@router.get(
    "/cosmo",
    response_model=GraphCosmoOut,
    summary="Cosmo canvas dataset: project super-nodes + aggregated cross-project edges",
)
@limiter.limit("30/minute")
async def graph_cosmo(
    request: Request,
    user: UserInfo = Depends(get_current_user_or_agent),
    db: aiosqlite.Connection = Depends(get_db),
) -> GraphCosmoOut:
    """Bundle per il canvas `/graph`: project super-nodi + aggregated edges.

    Ogni project ha: degree (count edges uscenti cross-project), satellites
    top-8 per recency (kind derivato dal prefix node_id). Edges scartati se
    uno degli endpoint non e' visibile all'utente. Rate limit 30/min.

    ``graph_cosmo_service.fetch_cosmo_graph`` is fully self-contained (it does its
    own get_visible_projects internally and takes the raw UserInfo), so this
    handler stays a thin pass-through.
    """
    return await graph_cosmo_service.fetch_cosmo_graph(db, user)
