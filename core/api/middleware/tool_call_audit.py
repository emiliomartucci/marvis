# v0.0.0 - 2026-04-16 - KG Phase 6.5: audit middleware for MCP tool calls
"""FastAPI middleware that writes one audit_log row per MCP tool call.

Scope: only paths that are exposed as MCP tools (search, graph_*, kg_*,
session_brief, handoffs, learnings CRUD, etc.). We do NOT audit every
HTTP request — that would flood the table. The prefix allowlist is
maintained in ``_MCP_PREFIXES``.

Row shape:
    action        = "tool_call"
    user          = X-Agent-Name header (or "anonymous")
    resource_type = first path segment after /api/v1/ (e.g. "search", "graph")
    resource_id   = remaining path (so distinct tools stay distinct)
    details_json  = {"method": "GET", "status": 200, "elapsed_ms": 42}

Non-blocking: failures log a warning and never interrupt the response.
"""
from __future__ import annotations

import logging
import time

import aiosqlite
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from core.api.config import settings
from core.api.services.audit import log_audit

logger = logging.getLogger(__name__)

# Keep this list tight — every prefix here writes one audit row per request.
_MCP_PREFIXES: tuple[str, ...] = (
    "/api/v1/search",
    "/api/v1/graph",
    "/api/v1/kg",
    "/api/v1/projects/",  # matches /brief, /handoffs — granular below
    "/api/v1/handoffs",
    "/api/v1/learnings",
    "/api/v1/costs",
    "/api/v1/monitoring",
)

# Exclude extremely chatty sub-paths that are not MCP-exposed.
_EXCLUDE_SUBSTRINGS: tuple[str, ...] = (
    "/sessions",
    "/pull_requests",
    "/tags",
    "/files",
    "/share",
)


def _should_audit(path: str) -> bool:
    if not any(path.startswith(p) for p in _MCP_PREFIXES):
        return False
    return not any(sub in path for sub in _EXCLUDE_SUBSTRINGS)


class MCPToolCallAuditMiddleware(BaseHTTPMiddleware):
    """Log a row in ``audit_log`` for each MCP-exposed HTTP request.

    See module docstring for row shape and exclusions.
    """

    async def dispatch(self, request: Request, call_next):
        if not _should_audit(request.url.path):
            return await call_next(request)

        start = time.perf_counter()
        response: Response = await call_next(request)
        elapsed_ms = int((time.perf_counter() - start) * 1000)

        try:
            await self._record(request, response, elapsed_ms)
        except Exception as exc:  # noqa: BLE001 — never block the response
            logger.warning("MCPToolCallAudit write failed: %s", exc)
        return response

    async def _record(
        self,
        request: Request,
        response: Response,
        elapsed_ms: int,
    ) -> None:
        agent = getattr(request.state, "auth_username", "anonymous")
        workspace_id = getattr(request.state, "auth_workspace_id", None)
        if not isinstance(workspace_id, str) or not workspace_id.strip():
            # An unauthenticated/failed request has no tenant chain. Never
            # misattribute it to the default workspace; a separate global
            # security sink may record it at the ingress boundary.
            return
        path = request.url.path
        # Extract the first segment after /api/v1/ for resource_type.
        tail = path.removeprefix("/api/v1/")
        head = tail.split("/", 1)[0] if "/" in tail else tail
        resource_type = head or "mcp"
        resource_id = tail

        db = await aiosqlite.connect(settings.db_path)
        try:
            await db.execute("BEGIN IMMEDIATE")
            await log_audit(
                db,
                action="tool_call",
                user=agent,
                resource_type=resource_type,
                resource_id=resource_id,
                details={
                    "method": request.method,
                    "status": response.status_code,
                    "elapsed_ms": elapsed_ms,
                    "has_query": bool(request.url.query),
                },
                workspace_id=workspace_id,
            )
            await db.commit()
        except Exception:
            await db.rollback()
            raise
        finally:
            await db.close()
