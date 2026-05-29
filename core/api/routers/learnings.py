# v2.0.0 - 2026-05-27 - S1 F1.1: thin adapter over use_cases.learnings (TEMPLATE router)
# v1.1.0 - 2026-04-16 - Phase 7.0 KG Inline Lens: ?deep=true on get/list/check + H4 visibility guard
"""HTTP adapter for the learnings domain (S1 collapse-runtime TEMPLATE).

This router is a thin transport adapter. All CRUD/search/validation/RBAC/
visibility logic lives in :mod:`core.api.use_cases.learnings` (pure, fastapi-free).
Each handler:

1. resolves identity into a :class:`CallerContext` (``from_user_info``; learnings
   has no human-only gate, so ``is_human_session=False`` — no ``Request`` needed);
2. for ``list``, resolves visibility at the boundary (DECISION 1) and passes it in;
3. calls the use_case inside ``try/except ServiceError`` -> ``to_http``;
4. owns everything ``deep``-related: the ``deep_requires_filter`` 400 guard
   (DECISION 3) and the rate-limit + audit + ``kg_context`` enrichment (DECISION 2).

The pure helpers and DTOs are re-exported from the use_case so existing importers
(``main.py`` for ``router``; ``services``/tests for the helpers) keep working.
"""
from __future__ import annotations

import asyncio
import logging

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from core.api.db import get_db, get_write_db
from core.api.models import UserInfo
from core.api.rbac import require_role
from core.api.routers._adapter import to_http
from core.api.security import get_current_user_or_agent
from core.api.services.kg.audit import check_deep_rate_limit, log_kg_deep_access
from core.api.services.kg.lens import build_kg_context_for_learning
from core.api.use_cases import learnings as uc
from core.api.use_cases._context import CallerContext
from core.api.use_cases._errors import ServiceError
from core.api.visibility import get_visible_projects

# Re-export the domain DTOs + pure helpers + constants from the use_case so that
# (a) `response_model=` below references the same classes and (b) existing
# importers — `services/kg/lens.py`, `core/api/tests/test_learnings_check_search.py`
# — keep importing them from this router path unchanged.
from core.api.use_cases.learnings import (  # noqa: F401  (re-export surface)
    CHECK_LEARNINGS_STOPWORDS,
    VALID_CATEGORIES,
    VALID_SEVERITIES,
    LearningCheckResponse,
    LearningResponse,
    _extract_check_terms,
    _learning_match_score,
    _row_to_learning,
    _search_learning_rows,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/learnings", tags=["learnings"])


# ---------------------------------------------------------------------------
# HTTP request models (transport contract — these STAY in the router)
# ---------------------------------------------------------------------------


class LearningCreateRequest(BaseModel):
    title: str
    category: str
    description: str
    tags: list[str] = Field(default_factory=list)
    module: str | None = None
    severity: str = "medium"
    prevention: str | None = None
    session: int | None = None
    project: str | None = None


class LearningUpdateRequest(BaseModel):
    title: str | None = None
    category: str | None = None
    description: str | None = None
    tags: list[str] | None = None
    module: str | None = None
    severity: str | None = None
    prevention: str | None = None
    session: int | None = None
    project: str | None = None


# ---------------------------------------------------------------------------
# Endpoints (thin adapters)
# ---------------------------------------------------------------------------


@router.get("/check", response_model=LearningCheckResponse)
async def check_learnings(
    q: str = Query(
        ..., description="Keyword to search in title, description, tags, module"
    ),
    module: str | None = Query(
        None, description="Filter by module (exact or LIKE match)"
    ),
    deep: bool = Query(False, description="Attach KG context to each result (Phase 7.0)"),
    user: UserInfo = Depends(get_current_user_or_agent),
    db: aiosqlite.Connection = Depends(get_db),
) -> LearningCheckResponse:
    """Search learnings relevant to a keyword/module. Used by MCP before risky actions."""
    ctx = CallerContext.from_user_info(user, is_human_session=False)
    try:
        result = await uc.check_learnings(ctx, db, query=q, module=module)
    except ServiceError as e:
        raise to_http(e)

    # DECISION 2: deep KG enrichment is a per-surface adapter concern.
    if deep and result.results:
        check_deep_rate_limit(user.username)
        log_kg_deep_access(user.username, "check_learnings", q)
        kg_contexts = await asyncio.gather(
            *[build_kg_context_for_learning(db, r.id, deep=True) for r in result.results],
            return_exceptions=True,
        )
        for r, kg_ctx in zip(result.results, kg_contexts):
            if not isinstance(kg_ctx, Exception):
                r.kg_context = kg_ctx

    return result


@router.get("", response_model=list[LearningResponse])
async def list_learnings(
    category: str | None = None,
    project: str | None = None,
    severity: str | None = None,
    tags: str | None = Query(None, description="Comma-separated tags (OR logic)"),
    search: str | None = Query(None, description="Free text search"),
    module: str | None = Query(None, description="Filter by module"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    deep: bool = Query(False, description="Attach KG context (aggregate deferred to Phase 7.1)"),
    user: UserInfo = Depends(get_current_user_or_agent),
    db: aiosqlite.Connection = Depends(get_db),
) -> list[LearningResponse]:
    """List learnings with filters."""
    # DECISION 3: deep_requires_filter is an adapter-owned guard tied to the
    # adapter-owned `deep` feature. Keep the exact 400 (NOT via ServiceError).
    if deep and not project and not module and not search:
        raise HTTPException(
            status_code=400,
            detail="deep=true requires ?project=, ?module=, or ?search= filter",
        )

    # DECISION 1 (the visibility template): resolve visibility at the boundary
    # (needs UserInfo.teams, not carried by CallerContext) and pass it in; the
    # use_case enforces it. Only resolve when a project filter is present.
    visible_projects = await get_visible_projects(db, user) if project else None

    ctx = CallerContext.from_user_info(user, is_human_session=False)
    try:
        return await uc.list_learnings(
            ctx,
            db,
            category=category,
            project=project,
            severity=severity,
            tags=tags,
            search=search,
            module=module,
            limit=limit,
            offset=offset,
            visible_projects=visible_projects,
        )
    except ServiceError as e:
        raise to_http(e)


@router.get("/{learning_id}", response_model=LearningResponse)
async def get_learning(
    learning_id: str,
    deep: bool = Query(False, description="Attach KG context (Phase 7.0)"),
    user: UserInfo = Depends(get_current_user_or_agent),
    db: aiosqlite.Connection = Depends(get_db),
) -> LearningResponse:
    """Get a single learning by ID."""
    ctx = CallerContext.from_user_info(user, is_human_session=False)
    try:
        result = await uc.get_learning(ctx, db, learning_id=learning_id)
    except ServiceError as e:
        raise to_http(e)

    # DECISION 2: deep KG enrichment is a per-surface adapter concern.
    if deep:
        check_deep_rate_limit(user.username)
        log_kg_deep_access(user.username, "get_learning", learning_id)
        result.kg_context = await build_kg_context_for_learning(db, learning_id, deep=True)
    return result


@router.post("", response_model=LearningResponse, status_code=201)
async def create_learning(
    body: LearningCreateRequest,
    user: UserInfo = Depends(require_role("operator", "admin", "super_admin")),
    db: aiosqlite.Connection = Depends(get_write_db),
) -> LearningResponse:
    """Create a new learning."""
    ctx = CallerContext.from_user_info(user, is_human_session=False)
    try:
        return await uc.create_learning(
            ctx,
            db,
            title=body.title,
            category=body.category,
            description=body.description,
            tags=body.tags,
            module=body.module,
            severity=body.severity,
            prevention=body.prevention,
            session=body.session,
            project=body.project,
        )
    except ServiceError as e:
        raise to_http(e)


@router.patch("/{learning_id}", response_model=LearningResponse)
async def update_learning(
    learning_id: str,
    body: LearningUpdateRequest,
    user: UserInfo = Depends(require_role("operator", "admin", "super_admin")),
    db: aiosqlite.Connection = Depends(get_write_db),
) -> LearningResponse:
    """Update a learning."""
    ctx = CallerContext.from_user_info(user, is_human_session=False)
    # exclude_unset preserves the HTTP semantic (omitted = unchanged, null = clear),
    # equivalent to the previous `body.model_fields_set` gating.
    fields = body.model_dump(exclude_unset=True)
    try:
        return await uc.update_learning(ctx, db, learning_id=learning_id, fields=fields)
    except ServiceError as e:
        raise to_http(e)


@router.post("/{learning_id}/bump", response_model=LearningResponse)
async def bump_learning(
    learning_id: str,
    user: UserInfo = Depends(require_role("operator", "admin", "super_admin")),
    db: aiosqlite.Connection = Depends(get_write_db),
) -> LearningResponse:
    """Increment frequency and update last_occurrence. Used when the same issue recurs."""
    ctx = CallerContext.from_user_info(user, is_human_session=False)
    try:
        return await uc.bump_learning(ctx, db, learning_id=learning_id)
    except ServiceError as e:
        raise to_http(e)
