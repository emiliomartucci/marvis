# v1.0.0 - 2026-08-05 - Plan 2 U1: graph ingest router (thin transport)
"""HTTP surface for the graph ingest contract.

Thin adapter over ``use_cases.graph_ingest``: resolve identity into a
``CallerContext``, call the pure use_case, map ``ServiceError`` to HTTP via the
same ``to_http_legacy`` the graph read router uses. Registered on the same
``/api/v1/graph`` prefix as the read endpoints.
"""
from __future__ import annotations

import aiosqlite
from fastapi import APIRouter, Depends

from core.api.db import get_db, get_write_db
from core.api.models import UserInfo
from core.api.models.graph_ingest import GraphIngestRequest
from core.api.routers.graph import to_http_legacy
from core.api.security import get_current_user_or_agent
from core.api.use_cases import graph_ingest as uc
from core.api.use_cases._context import CallerContext
from core.api.use_cases._errors import ServiceError

router = APIRouter(prefix="/api/v1/graph", tags=["graph"])


@router.post("/ingest")
async def graph_ingest_endpoint(
    request: GraphIngestRequest,
    current_user: UserInfo = Depends(get_current_user_or_agent),
    db: aiosqlite.Connection = Depends(get_write_db),
) -> dict:
    """Atomically replace one project's code graph from a parsed batch.

    The tenant stores nodes/edges + provenance, never the source. Operator+.
    """
    ctx = CallerContext.from_user_info(current_user, is_human_session=False)
    try:
        return await uc.ingest_graph(ctx, db, request)
    except ServiceError as e:
        raise to_http_legacy(e)


@router.get("/provenance/{project}")
async def graph_provenance_endpoint(
    project: str,
    current_user: UserInfo = Depends(get_current_user_or_agent),
    db: aiosqlite.Connection = Depends(get_db),
) -> dict:
    """Return how a project's active graph arrived (source, commit, dirty, age)."""
    ctx = CallerContext.from_user_info(current_user, is_human_session=False)
    try:
        return await uc.read_provenance(ctx, db, project)
    except ServiceError as e:
        raise to_http_legacy(e)
