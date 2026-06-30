# v1.0.0 - 2026-06-26 - Agent-facing MCP route cheatsheet.
"""Guide MCP tool."""
from __future__ import annotations

from typing import Any

from core.api.mcp.guidance import guide_payload


def register(mcp) -> None:
    """Register the guide tool on the shared FastMCP instance."""

    @mcp.tool()
    async def guide() -> dict[str, Any]:
        """Concise routing cheatsheet for connected agents.

        WHEN TO USE: first call when you are unsure which Marvis MCP tool fits the user's intent. First-time agents should then call agent_onboarding_guide for AGENTS.md/CLAUDE.md/Codex rule patches.
        WHEN NOT TO USE: not for project data itself; follow the returned intent->tool map.
        RETURNS: {routing:[{intent, tool, why}], rules[], agent_hint}."""
        return guide_payload()
