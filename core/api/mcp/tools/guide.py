# v1.1.0 - 2026-07-03 - Agent-facing MCP route cheatsheet (P2 de-hijacked hint).
"""Guide MCP tool."""
from __future__ import annotations

from typing import Any

from core.api.mcp.guidance import guide_payload


def register(mcp) -> None:
    """Register the guide tool on the shared FastMCP instance."""

    @mcp.tool()
    async def guide() -> dict[str, Any]:
        """Concise routing cheatsheet for connected agents.

        WHEN TO USE: first call when you are unsure which Marvis MCP tool fits the
        user's intent. First-time agents should then call agent_onboarding_guide
        for hosted-canonical setup, then onboarding_status for the guided wizard.
        WHEN NOT TO USE: not for project data itself; follow the returned
        intent->tool map.
        RETURNS: {routing:[{intent, tool, why}], rules[], agent_hint}."""
        return guide_payload()
