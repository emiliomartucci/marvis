# v1.0.0 - 2026-03-13 - Workspace resolution via FastAPI dependency chain
"""Tenant resolution dependency.

Workspace is derived from the authenticated user (JWT claim or token lookup),
NEVER from a request header (spoofable).
NEVER from ContextVar in background tasks (doesn't propagate).

Usage in routers:
    @router.get("/items")
    async def list_items(
        workspace_id: str = Depends(get_workspace_id),
        db: aiosqlite.Connection = Depends(get_db),
    ):
        rows = await db.execute("SELECT * FROM items WHERE workspace_id = ?", (workspace_id,))
"""
from __future__ import annotations

from fastapi import Depends

from core.api.models import UserInfo
from core.api.security import get_current_user_or_agent


async def get_workspace_id(
    user: UserInfo = Depends(get_current_user_or_agent),
) -> str:
    """Resolve workspace from authenticated user.

    The workspace_id is set during authentication:
    - Cookie auth: from JWT claim or users.workspace_id
    - Bearer auth: from agent_tokens.workspace_id
    - Legacy token: hardcoded to ws_default
    """
    return user.workspace_id or "ws_default"
