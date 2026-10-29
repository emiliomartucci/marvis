"""Tool gateway: ``find_tool`` + ``call_tool`` for the tools the diet hides.

The tool diet keeps ``tools/list`` to the data-driven core, but an agent never
calls a tool it cannot see: in task 2951f474 the hidden tools were used 0 times
out of 51 requests. These two tools make them discoverable again. The
description of ``find_tool`` is rewritten per request by
``ToolProfilesMiddleware`` to name the tools hidden for the caller's role — the
names are the lever (35-59% → hidden-tool success on Haiku, 57% on Sonnet, core
accuracy unchanged).

HTTP only: ``server.py`` registers the gateway on the hosted server, never on the
stdio server, which has no diet. ``call_tool`` dispatches through
``server.call_tool`` so the role gate, Enterprise strict identity checks and the
usage counter run for the inner tool exactly as for a direct call.

Kill-switch: ``MARVIS_TOOL_GATEWAY=0`` hides both tools from ``tools/list``.
"""
from __future__ import annotations

from typing import Annotated, Any

from fastmcp.exceptions import NotFoundError, ToolError
from pydantic import Field

from core.api.mcp.tool_gateway_names import (  # noqa: E402
    _GATEWAY_META,
    _RESULT_DESCRIPTION_CHARS,
    CALL_TOOL,
    CALL_TOOL_DESCRIPTION,
    FIND_TOOL,
    FIND_TOOL_DESCRIPTION,
    GATEWAY_TOOLS,
    find_tool_description,
    gateway_enabled,
)

__all__ = ["GATEWAY_TOOLS", "find_tool_description", "gateway_enabled", "register_tool_gateway"]


def register_tool_gateway(server: Any) -> None:
    """Register ``find_tool`` and ``call_tool`` on the hosted FastMCP server."""
    from core.api.mcp import tool_profiles
    from core.api.mcp.tool_search import rank_tools

    @server.tool(name=FIND_TOOL, description=FIND_TOOL_DESCRIPTION, meta=_GATEWAY_META)
    async def find_tool(
        query: Annotated[str, Field(description="What you need to do, or a tool name")],
        limit: Annotated[int, Field(ge=1, le=10, description="How many tools to return")] = 5,
    ) -> dict:
        tools = await server.list_tools(run_middleware=False)
        callable_tools = [
            tool
            for tool in tools
            if tool.name not in GATEWAY_TOOLS and tool_profiles.caller_may_call(tool.name)
        ]
        hits = rank_tools(query, callable_tools, limit=limit)
        return {
            "tools": [
                {
                    "name": tool.name,
                    "description": (tool.description or "")[:_RESULT_DESCRIPTION_CHARS],
                    "inputSchema": tool.parameters,
                }
                for tool in hits
            ]
        }

    @server.tool(
        name=CALL_TOOL, description=CALL_TOOL_DESCRIPTION, output_schema=None, meta=_GATEWAY_META
    )
    async def call_tool(
        name: Annotated[str, Field(description="Tool name returned by find_tool")],
        arguments: Annotated[
            dict[str, Any] | None, Field(description="The tool's input object")
        ] = None,
    ):
        if name in GATEWAY_TOOLS:
            raise ToolError(f"call_tool cannot run '{name}'; call it directly.")
        try:
            return await server.call_tool(name, arguments or {})
        except NotFoundError as exc:
            raise ToolError(f"Unknown tool: {name}") from exc

