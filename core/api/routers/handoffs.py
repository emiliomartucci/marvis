# v2.0.0 - 2026-05-27 - S1 F1.11: thin adapter over use_cases.handoffs
# v1.3.1 - 2026-04-17 - fix node_id double prefix in deep handoff bundle
"""HTTP adapter for the handoffs domain (S1 collapse-runtime).

This router is a thin transport adapter. All search/read/validation/visibility
logic lives in :mod:`core.api.use_cases.handoffs` (pure, fastapi-free). Each
handler:

1. resolves identity into a :class:`CallerContext` (handoffs have no human-only
   gate, so ``is_human_session=False`` — no ``Request`` needed);
2. resolves visibility at the boundary (DECISION 1) and passes it in;
3. calls the use_case inside ``try/except ServiceError`` -> ``to_http``;
4. owns the ``deep`` KG enrichment: rate-limit + audit log + ``kg_context``
   (DECISION 2).

``get_current_user_or_agent`` stays as the ``Depends`` on ``search_handoffs`` so
the agent-facing auth regression test (``test_agent_facing_auth_dependencies``)
keeps passing. The pure helpers + DTOs are re-exported from the use_case so any
existing importer keeps working.
"""
from __future__ import annotations

import logging

import aiosqlite
from fastapi import APIRouter, Depends, Query

from core.api.db import get_db, get_vec_db
from core.api.models import HandoffSearchResult, UserInfo
from core.api.rbac import require_role
from core.api.routers._adapter import to_http
from core.api.security import get_current_user_or_agent
from core.api.services.kg.audit import check_deep_rate_limit, log_kg_deep_access
from core.api.services.kg.lens import build_kg_context_for_handoff, require_kg_visibility
from core.api.use_cases import handoffs as uc
from core.api.use_cases._context import CallerContext
from core.api.use_cases._errors import ServiceError
from core.api.visibility import get_visible_projects

# Re-export the pure helpers + constants from the use_case so existing importers
# (and any future callers) keep importing them from this router path unchanged.
from core.api.use_cases.handoffs import (  # noqa: F401  (re-export surface)
    _HANDOFF_FILENAME_RE,
    _SNIPPET_RADIUS,
    _count_matches,
    _extract_snippet,
    _iter_all_slugs,
    _parse_frontmatter,
    _safe_read,
    _search_semantic,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/handoffs", tags=["handoffs"])


# ---------------------------------------------------------------------------
# Endpoints (thin adapters)
# ---------------------------------------------------------------------------


@router.get("/search", response_model=list[HandoffSearchResult])
async def search_handoffs(
    q: str | None = Query(None, description="Full-text query (searches filename, tags, body)"),
    project: str | None = Query(None, description="Filter by project slug"),
    tags: str | None = Query(None, description="Comma-separated tags (OR logic)"),
    date_start: str | None = Query(None, description="Start date filter YYYY-MM-DD (inclusive)"),
    date_end: str | None = Query(None, description="End date filter YYYY-MM-DD (inclusive)"),
    limit: int = Query(20, ge=1, le=200, description="Max results"),
    user: UserInfo = Depends(get_current_user_or_agent),
    db: aiosqlite.Connection = Depends(get_db),
    vec_db: aiosqlite.Connection = Depends(get_vec_db),
) -> list[HandoffSearchResult]:
    """Full-text search across handoff files.

    Parses frontmatter YAML from every handoff-*.md file found in project
    memory/ directories and filters by query, project, tags, and date range.
    Results are ranked by match count (descending), then date (descending).
    """
    # DECISION 1 (the visibility template): resolve visibility at the boundary
    # (needs UserInfo.teams, not carried by CallerContext) and pass it in; the
    # use_case enforces it by silent filtering, exactly as the original router did.
    visible_projects = await get_visible_projects(db, user)

    ctx = CallerContext.from_user_info(user, is_human_session=False)
    try:
        return await uc.search_handoffs(
            ctx,
            db,
            vec_db,
            q=q,
            project=project,
            tags=tags,
            date_start=date_start,
            date_end=date_end,
            limit=limit,
            visible_projects=visible_projects,
        )
    except ServiceError as e:
        raise to_http(e)


@router.get("/{project_slug}/{filename}")
async def get_handoff(
    project_slug: str,
    filename: str,
    deep: bool = Query(False, description="If true, append kg_context from KG Inline Lens"),
    user: UserInfo = Depends(get_current_user_or_agent),
    db: aiosqlite.Connection = Depends(get_db),
) -> dict:
    """Get a single handoff file by project slug and filename.

    Returns frontmatter (parsed YAML) + body text. With ?deep=true also
    returns kg_context built via build_kg_context_for_handoff().
    """
    # DECISION 1: resolve visibility at the boundary; the use_case raises
    # NotFoundError (404) when the project is not visible (mirrors the original
    # check_project_access — 404 not 403, does not reveal existence).
    visible_projects = await get_visible_projects(db, user)

    ctx = CallerContext.from_user_info(user, is_human_session=False)
    try:
        if deep:
            require_kg_visibility(ctx, visible_projects)
        result = await uc.get_handoff(
            ctx,
            db,
            project_slug=project_slug,
            filename=filename,
            visible_projects=visible_projects,
        )
    except ServiceError as e:
        raise to_http(e)

    # DECISION 2: deep KG enrichment is a per-surface adapter concern.
    if deep:
        check_deep_rate_limit(user.username)
        log_kg_deep_access(user.username, "get_handoff", filename)
        # Builder normalizes raw filename (strips .md + handoff- prefix, then
        # prepends handoff:artifact:). Do not pre-prefix here.
        result["kg_context"] = await build_kg_context_for_handoff(
            db,
            filename,
            deep=True,
            ctx=ctx,
            visible_projects=visible_projects,
        )

    return result


@router.post("/reindex")
async def reindex_handoffs(
    project: str = Query(..., description="Project slug to reindex"),
    user: UserInfo = Depends(require_role("admin", "super_admin")),  # Bearer-accessible for deploy scripts
    vec_db: aiosqlite.Connection = Depends(get_vec_db),
) -> dict:
    """On-demand reindex of handoff embeddings for a project. Admin only."""
    ctx = CallerContext.from_user_info(user, is_human_session=False)
    try:
        return await uc.reindex_handoffs(ctx, vec_db, project=project)
    except ServiceError as e:
        raise to_http(e)
