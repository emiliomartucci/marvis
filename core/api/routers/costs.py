# v2.0.0 - 2026-05-27 - S1 F1.3: thin adapter over use_cases.costs
# v1.1.0 - 2026-03-11 - Project visibility enforcement on cost endpoints (P0)
"""HTTP adapter for the costs domain (S1 collapse-runtime).

This router is a thin transport adapter. All cost-aggregation / query logic lives
in :mod:`core.api.use_cases.costs` (pure, fastapi-free). Each handler:

1. resolves identity into a :class:`CallerContext` (``from_user_info``; costs has
   no human-only gate, so ``is_human_session=False`` — no ``Request`` needed);
2. normalizes the date range at the boundary (``_resolve_date_range``, keeps its
   own 400 — analogue of the template's DECISION 3) and, for slug endpoints,
   resolves visibility (DECISION 1) and passes it in;
3. calls the use_case inside ``try/except ServiceError`` -> ``to_http``.

``_get_programs`` is re-exported here so the use_case's ``programs_loader`` arg
reads it at call time and existing tests can ``patch("api.routers.costs._get_programs")``.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException, Path as PathParam, Query

from core.api.db import get_db
from core.api.models import ConversationCost, ProjectBillingSummary, ProjectCostSummary, UserInfo

# Re-exported so the use_case's `programs_loader` reads the (patchable) module
# attribute at call time; tests patch `api.routers.costs._get_programs`.
from core.api.routers.projects import _get_programs  # noqa: F401  (re-export / patch seam)
from core.api.routers._adapter import to_http
from core.api.security import get_current_user_or_agent
from core.api.use_cases import costs as uc
from core.api.use_cases._context import CallerContext
from core.api.use_cases._errors import ServiceError
from core.api.visibility import get_visible_projects

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/costs", tags=["costs"])

MAX_DATE_RANGE_DAYS = 365


def _resolve_date_range(
    from_date: str | None, to_date: str | None
) -> tuple[str, str]:
    """Resolve and validate date range, auto-filling missing params.

    Input normalization at the transport boundary; keeps its own ``HTTPException(400)``
    (no domain ``ServiceError`` maps to 400) — analogue of the template's DECISION 3.
    """
    if from_date and not to_date:
        to_date = date.today().isoformat()
    if to_date and not from_date:
        from_date = (date.fromisoformat(to_date) - timedelta(days=MAX_DATE_RANGE_DAYS)).isoformat()
    if not from_date and not to_date:
        from_date = (date.today() - timedelta(days=30)).isoformat()
        to_date = date.today().isoformat()

    from_d = date.fromisoformat(from_date)
    to_d = date.fromisoformat(to_date)
    if from_d > to_d:
        raise HTTPException(400, "from_date must be <= to_date")
    if (to_d - from_d).days > MAX_DATE_RANGE_DAYS:
        raise HTTPException(400, f"Date range exceeds {MAX_DATE_RANGE_DAYS} days")

    return from_date, to_date


@router.get("/summary", response_model=list[ProjectCostSummary])
async def get_costs_summary(
    from_date: str | None = Query(None, alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to_date: str | None = Query(None, alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    _user: UserInfo = Depends(get_current_user_or_agent),
    db: aiosqlite.Connection = Depends(get_db),
) -> list[ProjectCostSummary]:
    """All projects with cost > 0, grouped by project_slug."""
    from_d, to_d = _resolve_date_range(from_date, to_date)
    ctx = CallerContext.from_user_info(_user, is_human_session=False)
    try:
        visible_projects = await get_visible_projects(db, _user)
        return await uc.get_costs_summary(
            ctx,
            db,
            from_date=from_d,
            to_date=to_d,
            programs_loader=_get_programs,
            visible_projects=visible_projects,
        )
    except ServiceError as e:
        raise to_http(e)


@router.get("/by-project/{slug}", response_model=list[ConversationCost])
async def get_project_costs(
    slug: str = PathParam(..., pattern=r"^[a-z0-9][a-z0-9&+_.\-]{0,62}$"),
    from_date: str | None = Query(None, alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to_date: str | None = Query(None, alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    _user: UserInfo = Depends(get_current_user_or_agent),
    db: aiosqlite.Connection = Depends(get_db),
) -> list[ConversationCost]:
    """Conversation costs for a specific project, newest first."""
    from_d, to_d = _resolve_date_range(from_date, to_date)
    # DECISION 1 (visibility template): resolve visibility at the boundary
    # (needs UserInfo.teams/user_id, not carried by CallerContext) and pass it in;
    # the use_case enforces it (404 on a non-visible slug, does not reveal existence).
    ctx = CallerContext.from_user_info(_user, is_human_session=False)
    try:
        visible_projects = await get_visible_projects(db, _user)
        return await uc.get_project_costs(
            ctx,
            db,
            slug=slug,
            from_date=from_d,
            to_date=to_d,
            limit=limit,
            offset=offset,
            visible_projects=visible_projects,
        )
    except ServiceError as e:
        raise to_http(e)


@router.get("/billing/{slug}", response_model=ProjectBillingSummary)
async def get_project_billing(
    slug: str = PathParam(..., pattern=r"^[a-z0-9][a-z0-9&+_.\-]{0,62}$"),
    from_date: str | None = Query(None, alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to_date: str | None = Query(None, alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    _user: UserInfo = Depends(get_current_user_or_agent),
    db: aiosqlite.Connection = Depends(get_db),
) -> ProjectBillingSummary:
    """Billing summary from task_cost_entries for a project, aggregated by date range."""
    from_d, to_d = _resolve_date_range(from_date, to_date)
    ctx = CallerContext.from_user_info(_user, is_human_session=False)
    try:
        visible_projects = await get_visible_projects(db, _user)
        return await uc.get_project_billing(
            ctx,
            db,
            slug=slug,
            from_date=from_d,
            to_date=to_d,
            visible_projects=visible_projects,
        )
    except ServiceError as e:
        raise to_http(e)
