# v1.1.0 - 2026-07-03 - Agent-facing MCP route cheatsheet (P2 de-hijacked hint).
"""Guide MCP tool."""
from __future__ import annotations

from typing import Any

from core.api.mcp.guidance import guide_payload


def register(mcp) -> None:
    """Register the guide tool on the shared FastMCP instance."""

    @mcp.tool()
    async def guide(query: str = "") -> dict[str, Any]:
        """Concise routing cheatsheet for connected agents.

        WHEN TO USE: first call when you are unsure which Marvis MCP tool fits the
        user's intent. First-time agents should then call agent_onboarding_guide
        for hosted-canonical setup, then onboarding_status for the guided wizard.
        WHEN NOT TO USE: not for project data itself; follow the returned
        intent->tool map.
        Pass query for optional intent advice; it never invokes the suggested tool.
        RETURNS: {routing:[{intent, tool, why}], rules[], agent_hint, intent_advisory?}."""
        result = guide_payload()
        if query.strip():
            from core.api.services.jev.guardrail import dispatch_intent, DispatchResult
            try:
                advice = await dispatch_intent(query)
            except Exception:
                advice = DispatchResult("guide", ["search", "session_brief"], outcome="error")
            result["intent_advisory"] = advice.to_dict()
        return result
