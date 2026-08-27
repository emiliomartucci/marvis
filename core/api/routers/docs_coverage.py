"""Docs coverage API backed by KG `describes` edges."""
# v1.1.0 - 2026-05-15 - Accept X-Agent-Name as read-only operator auth (no Bearer required)
from __future__ import annotations

import json
from typing import Any

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel

from core.api.db import get_db
from core.api.models import UserInfo
from core.api.rbac import ROLE_HIERARCHY
from core.api.security import (
    _resolve_agent_userinfo,
    _valid_static_agent_names,
    get_current_user_or_agent,
    get_valid_agent_names,
)

router = APIRouter(prefix="/api/v1/docs", tags=["docs"])

# Allowlist semantics: docs/coverage is read-only telemetry. We accept the agent
# header alone (without Bearer) for the narrow case of internal Monitors running
# on the same host that cannot easily carry a cookie or token. The action label
# below is informational — it documents the only operation an agent-header-only
# caller may perform on this router.
_OPERATOR_ALLOWED_DOCS_COVERAGE_ACTIONS = {"coverage:read"}

LAYER_PATH_FILTERS: dict[str, tuple[str, ...]] = {
    "api": ("core/api/%", "api/%"),
    "mcp": ("core/mcp-pir/%", "mcp-pir/%", "mcp_server.py"),
    "llm-gateway": (
        "core/api/services/ingest/llm/%",
        "core/api/services/ingest/parsers/gateway_aux.py",
        "api/services/ingest/llm/%",
        "api/services/ingest/parsers/gateway_aux.py",
    ),
    "kg": (
        "core/api/routers/graph.py",
        "core/api/routers/kg.py",
        "core/scripts/%kg%",
        "core/scripts/populate_%",
        "api/routers/graph.py",
        "api/routers/kg.py",
        "scripts/%kg%",
        "scripts/populate_%",
    ),
}


class CoverageResponse(BaseModel):
    layer: str
    audience: str | None = None
    total_nodes: int
    documented: int
    undocumented_sample: list[str]
    coverage_pct: float
    corpus_state: str


async def require_docs_coverage_read(
    request: Request,
    db: aiosqlite.Connection = Depends(get_db),
) -> UserInfo:
    """Dual auth + header fallback for docs/coverage read-only access.

    Resolution order:
    1. If Authorization Bearer or pir_session cookie is present -> delegate to
       get_current_user_or_agent (existing behaviour, no breaking change).
    2. Else, if X-Agent-Name maps to a known agent user with role >= operator,
       authenticate as that agent (read-only, attribution-only). This unblocks
       trusted internal monitors that cannot carry a
       cookie/token but run on a trusted host.

    Operator agents reaching this endpoint via path (2) get read-only access by
    construction: this router only exposes a GET. The dependency MUST NOT be
    reused on write endpoints without re-checking the resolution path.
    """
    has_bearer = request.headers.get("authorization", "").startswith("Bearer ")
    has_cookie = bool(request.cookies.get("pir_session"))
    if has_bearer or has_cookie:
        return await get_current_user_or_agent(request, db)

    agent_name = request.headers.get("x-agent-name", "").strip()
    if not agent_name:
        raise HTTPException(
            status_code=401,
            detail=(
                "Not authenticated. Reason: no 'pir_session' cookie, no Bearer token, "
                "and no X-Agent-Name header on the request. "
                "Fix: POST /api/v1/auth/login (cookie), set Authorization: Bearer <token> "
                "(agent), or set X-Agent-Name: <slug> for read-only operator access "
                "from an internal Monitor."
            ),
        )

    valid_names = await get_valid_agent_names(db)
    if agent_name not in valid_names and agent_name not in _valid_static_agent_names():
        raise HTTPException(
            status_code=401,
            detail=(
                f"Unknown agent '{agent_name}'. Reason: X-Agent-Name does not match any "
                "active row in users(type='agent') nor a static system identity. "
                "Fix: use a registered agent slug (e.g. 'marvisx') or supply "
                "a Bearer token / pir_session cookie."
            ),
        )

    user = await _resolve_agent_userinfo(agent_name, db)
    if ROLE_HIERARCHY.get(user.system_role, -1) < ROLE_HIERARCHY["operator"]:
        raise HTTPException(
            status_code=403,
            detail=(
                "Insufficient permissions for X-Agent-Name fallback. Reason: agent "
                f"'{agent_name}' resolves to role '{user.system_role}', which is below "
                "the operator threshold required for header-only read access. "
                "Fix: use a Bearer token, log in via cookie, or have an admin promote "
                "the agent to operator role."
            ),
        )

    return user


def _metadata_matches_audience(metadata_raw: str | None, audience: str | None) -> bool:
    if not audience:
        return True
    try:
        metadata = json.loads(metadata_raw or "{}")
    except (TypeError, ValueError):
        return False
    audiences = metadata.get("audience") or []
    return audience in audiences


async def _target_nodes(db: aiosqlite.Connection, layer: str) -> list[dict[str, Any]]:
    path_filters = LAYER_PATH_FILTERS.get(layer)
    if not path_filters:
        return []

    conditions = " OR ".join("file_path LIKE ?" for _ in path_filters)
    cursor = await db.execute(
        f"""
        SELECT id, file_path
          FROM graph_nodes
         WHERE project_id = 'marvisx'
           AND deprecated_at IS NULL
           AND type IN ('function', 'file', 'module')
           AND ({conditions})
         ORDER BY degree DESC, id ASC
        """,
        path_filters,
    )
    return [dict(row) for row in await cursor.fetchall()]


@router.get(
    "/coverage",
    response_model=CoverageResponse,
)
async def docs_coverage(
    layer: str = Query(pattern="^(api|mcp|llm-gateway|kg)$"),
    audience: str | None = Query(default=None, pattern="^(integrator|agent|operator|internal)$"),
    _user: UserInfo = Depends(require_docs_coverage_read),
    db: aiosqlite.Connection = Depends(get_db),
) -> CoverageResponse:
    """Return KG-native documentation coverage for a machine-derived layer.

    Auth: pir_session cookie, Bearer token, or X-Agent-Name header mapping to an
    operator-or-higher agent (read-only, attribution-only). See
    `require_docs_coverage_read` for the resolution order.
    """
    nodes = await _target_nodes(db, layer)
    if not nodes:
        return CoverageResponse(
            layer=layer,
            audience=audience,
            total_nodes=0,
            documented=0,
            undocumented_sample=[],
            coverage_pct=0.0,
            corpus_state="bootstrap",
        )

    node_ids = [row["id"] for row in nodes]
    placeholders = ",".join("?" for _ in node_ids)
    cursor = await db.execute(
        f"""
        SELECT DISTINCT e.target_id, n.metadata
          FROM graph_edges e
          JOIN graph_nodes n ON n.id = e.source_id
         WHERE e.relation = 'describes'
           AND e.valid_until IS NULL
           AND e.target_id IN ({placeholders})
           AND n.deprecated_at IS NULL
        """,
        node_ids,
    )
    documented = {
        row["target_id"]
        for row in await cursor.fetchall()
        if _metadata_matches_audience(row["metadata"], audience)
    }
    total = len(node_ids)
    documented_count = len(documented)
    undocumented = [node_id for node_id in node_ids if node_id not in documented]
    return CoverageResponse(
        layer=layer,
        audience=audience,
        total_nodes=total,
        documented=documented_count,
        undocumented_sample=undocumented[:10],
        coverage_pct=round((documented_count / total) * 100, 2) if total else 0.0,
        corpus_state="ready" if total >= 5 else "bootstrap",
    )
