"""Agent-facing onboarding tools for hosted Marvis MCP."""
from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import Field

from core.api.mcp.guidance import agent_onboarding_payload

Detail = Literal["compact", "standard"]


def register(mcp) -> None:
    """Register agent onboarding tools on the shared FastMCP instance."""

    @mcp.tool()
    async def agent_onboarding_guide(
        client: Annotated[str, Field(max_length=80)] = "unknown",
        project_slug: Annotated[str | None, Field(max_length=80)] = None,
        current_instructions_excerpt: Annotated[
            str | None, Field(max_length=4000)
        ] = None,
        issue: Annotated[str | None, Field(max_length=1000)] = None,
        detail: Detail = "standard",
    ) -> dict[str, Any]:
        """First-time agent setup guide with concrete instruction patches.

        QUANDO USARLO: prima connessione di Codex/Claude/altro agente a Marvis hosted, oppure quando AGENTS.md/CLAUDE.md/config locali possono contraddire hosted. PROVA: ritorna patch concrete alle istruzioni e check canonicality.
        QUANDO NON USARLO: NOT per stato progetto -> session_brief; NOT per dati del brain -> search/read_file/grep.
        NEXT: applica mentalmente la patch o proponila all'utente, poi session_brief(project_slug)."""
        return agent_onboarding_payload(
            client=client,
            project_slug=project_slug,
            current_instructions_excerpt=current_instructions_excerpt,
            issue=issue,
            detail=detail,
        )
