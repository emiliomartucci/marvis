# v1.0.0 - 2026-08-18 - Per-tool usage-counter middleware for the hosted HTTP MCP surface.
"""Usage-counter middleware — the choke-point that feeds the per-tool measure.

One durable line per HTTP ``tools/call`` (``{tool, actor, ts}``), written through
:func:`core.api.services.tool_usage.record_tool_call`. Complements the ephemeral
journald ``marvis.tool_call`` line from :mod:`core.api.mcp.tool_profiles`: that
one is for live debugging and rotates away; this one is the durable, month-
aggregatable series the operator report reads.

Contract:
* Records EVERY call, success or error (usage = an attempted call), by recording
  in a ``finally`` around ``call_next``.
* Never blocks and never breaks the call: the recorder swallows its own failures,
  and this middleware additionally guards the record path so a bug here cannot
  turn a good tool result into an error.
* Logs NO arguments — only the tool name, the actor kind, and a timestamp.
* HTTP-only, like the sibling middlewares: the SDK stdio server has no middleware
  layer and is a trusted local surface.
"""
from __future__ import annotations

import logging
import os
from typing import Any

from fastmcp.server.middleware.middleware import Middleware

from core.api.services.tool_usage import record_tool_call

logger = logging.getLogger("marvis.tool_usage")


def _resolve_actor_kind() -> str:
    """``human`` | ``agent`` for the current caller; fail-safe to ``agent``."""
    try:
        from core.api.mcp._adapter import current_mcp_context

        return getattr(current_mcp_context(), "user_type", "agent") or "agent"
    except Exception:  # pragma: no cover - defense in depth
        return "agent"


def _record(context) -> None:  # noqa: ANN001
    tool_name = getattr(getattr(context, "message", None), "name", "") or ""
    tenant_id = (os.environ.get("TENANT_ID") or "").strip() or None
    record_tool_call(tool_name, _resolve_actor_kind(), tenant_id=tenant_id)


class ToolUsageMiddleware(Middleware):
    """Append one usage line per tool call without altering its result."""

    async def on_call_tool(self, context, call_next):  # noqa: ANN001
        try:
            return await call_next(context)
        finally:
            try:
                _record(context)
            except Exception:  # noqa: BLE001 - a counter must never break a tool call
                logger.debug("tool usage middleware record failed", exc_info=True)


def apply_tool_usage(server: Any) -> None:
    """Wire the durable usage counter onto the hosted HTTP MCP server."""
    server.add_middleware(ToolUsageMiddleware())
    logger.info("tool usage middleware wired")


__all__ = ["ToolUsageMiddleware", "apply_tool_usage"]
