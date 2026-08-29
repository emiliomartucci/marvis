"""Control-plane MCP tools for short-lived, route-confined credentials."""

from __future__ import annotations

from typing import Any

from core.api.mcp._adapter import (
    acquire_db,
    acquire_write_db,
    current_mcp_context,
    dump,
    raise_mcp_error,
    sync_oauth_user,
)
from core.api.use_cases import agent_tokens as agent_tokens_uc
from core.api.use_cases._errors import ServiceError


def register(mcp) -> None:
    """Register graph-ingest credential management on the shared MCP server."""

    @mcp.tool()
    async def mint_graph_ingest_token() -> dict[str, Any]:
        """Mint a one-hour bearer usable only for POST /api/v1/graph/ingest.

        Use this control-plane tool immediately before a local graph export. It
        never accepts or transports graph batches or source. The plaintext is
        returned once with exact scope `graph:ingest`; existing personal-token
        lifecycle endpoints remain the revocation path.

        Treat the plaintext as secret. Never copy it into logs, tasks,
        handoffs, source, or hosted documents.
        """
        try:
            ctx = current_mcp_context()
            async with acquire_db() as db:
                await sync_oauth_user(db, ctx)
            async with acquire_write_db(label="mcp.mint_graph_ingest_token") as db:
                result = await agent_tokens_uc.mint_graph_ingest_token(ctx, db)
                return dump(result)
        except ServiceError as exc:
            raise_mcp_error(exc)
