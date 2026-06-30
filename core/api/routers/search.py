# v2.0.0 - 2026-05-27 - S1 F1.2: thin adapter over use_cases.search
# v1.6.0 - 2026-04-16 - KG Phase 6.5 A: hybrid=true default (RRF fusion semantic + KG FTS5)
# v1.5.0 - 2026-04-12 - Add learnings, inbox_items, audits reindex; session_brief support
"""HTTP adapter for the search domain (S1 collapse-runtime, follows the learnings TEMPLATE).

This router is a thin transport adapter. All search/reindex/RBAC logic lives in
:mod:`core.api.use_cases.search` (pure, fastapi-free). Each handler:

1. resolves identity into a :class:`CallerContext` (``from_user_info``; search has
   no human-only gate, so ``is_human_session=False`` — no ``Request`` needed);
2. enforces the router-local per-identity rate limit (a transport concern — an
   in-memory token bucket on the HTTP edge, NOT a domain rule), then calls the
   use_case inside ``try/except ServiceError`` -> ``to_http``.

Template decisions on the search domain (see the use_case docstring for detail):
- DECISION 1 (visibility): N/A — search scopes by ``workspace_id`` only.
- DECISION 2 (``deep`` KG enrichment): N/A — no ``deep`` param.
- DECISION 3 (errors): the embedding-backend ``503`` ("Semantic search is temporarily unavailable")
  is a domain :class:`ServiceUnavailableError` raised by the use_case and mapped
  back to HTTP 503 by ``to_http``.

The pure helper ``_build_response`` and the reindex internals are re-exported from
the use_case so existing importers keep working unchanged.
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from core.api.models import UserInfo
from core.api.models.search import SearchResponse
from core.api.rbac import require_role
from core.api.routers._adapter import to_http
from core.api.security import get_current_user_or_agent
from core.api.use_cases import search as uc
from core.api.use_cases._context import CallerContext
from core.api.use_cases._errors import ServiceError

# Re-export the pure helper + reindex internals from the use_case so existing
# importers (tests / tooling that reach for `search._build_response`,
# `search._reindex_*`, `search._bg_tasks`) keep importing them from this router
# path unchanged.
from core.api.use_cases.search import (  # noqa: F401  (re-export surface)
    _bg_tasks,
    _build_response,
    _reindex_all_bg,
    _reindex_audits,
    _reindex_files,
    _reindex_handoffs,
    _reindex_inbox_items,
    _reindex_learnings,
    _reindex_projects,
    _reindex_tasks,
    _reindex_type,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/search", tags=["search"])


class ReindexPathsRequest(BaseModel):
    paths: list[str] = Field(..., min_length=1, max_length=100)


# --- Rate limiting (transport-only: in-memory token bucket on the HTTP edge) ---
_rate_limits: dict[str, list[float]] = defaultdict(list)
_RATE_LIMIT_RPM = 30
_RATE_LIMITS_MAX_KEYS = 1000


def _check_rate_limit(user: UserInfo) -> None:
    # user_id may be "" for legacy agent tokens — fall back to username
    identity = user.user_id or user.username
    now = time.monotonic()
    window = 60.0
    timestamps = _rate_limits[identity]
    _rate_limits[identity] = [t for t in timestamps if now - t < window]
    if len(_rate_limits[identity]) >= _RATE_LIMIT_RPM:
        raise HTTPException(status_code=429, detail="Rate limit exceeded (30 req/min)")
    _rate_limits[identity].append(now)
    if len(_rate_limits) > _RATE_LIMITS_MAX_KEYS:
        stale = [k for k, v in _rate_limits.items() if not v or (now - v[-1]) > window]
        for k in stale:
            del _rate_limits[k]


# ---------------------------------------------------------------------------
# Endpoints (thin adapters)
# ---------------------------------------------------------------------------


@router.get("", response_model=SearchResponse)
async def search(
    q: str = Query(..., min_length=1, max_length=500),
    hybrid: bool = Query(
        True,
        description="True (default): RRF fusion semantic + KG FTS5. False: semantic-only (legacy v1.5 behavior).",
    ),
    limit: int = Query(20, ge=1, le=50),
    as_of: str | None = Query(
        None,
        description=(
            "Point-in-time audit (Track 2 #1). ISO timestamp; reconstructs the "
            "learnings live at that instant. Only effective when "
            "MARVIS_TEMPORAL_MEMORY is enabled; ignored otherwise."
        ),
    ),
    user: UserInfo = Depends(get_current_user_or_agent),
) -> SearchResponse:
    """Hybrid (default) or semantic search across the KG and embedding index."""
    # Transport-only rate limit (router-local in-memory bucket), then domain call.
    _check_rate_limit(user)

    ctx = CallerContext.from_user_info(user, is_human_session=False)
    try:
        return await uc.search(ctx, q=q, hybrid=hybrid, limit=limit, as_of=as_of)
    except ServiceError as e:
        raise to_http(e)


@router.post("/reindex")
async def trigger_reindex(
    type: str = Query(
        "all",
        pattern="^(tasks|projects|files|handoffs|learnings|inbox_items|audits|all)$",
    ),
    user: UserInfo = Depends(require_role("operator", "admin", "super_admin")),
) -> dict:
    """Manual reindex. type=all returns immediately (background); specific type is synchronous."""
    ctx = CallerContext.from_user_info(user, is_human_session=False)
    try:
        return await uc.trigger_reindex(ctx, type=type)
    except ServiceError as e:
        raise to_http(e)


@router.post("/reindex-paths")
async def trigger_reindex_paths(
    body: ReindexPathsRequest,
    user: UserInfo = Depends(require_role("operator", "admin", "super_admin")),
) -> dict:
    """Manual delta reindex for explicit project file paths."""
    ctx = CallerContext.from_user_info(user, is_human_session=False)
    try:
        return await uc.reindex_file_paths(ctx, paths=body.paths)
    except ServiceError as e:
        raise to_http(e)
