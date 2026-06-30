# v1.5.0 - 2026-04-17 - P2: add filter_visible_edges DRY helper for cross-project RBAC
# v1.4.0 - 2026-04-16 - H3: env-controlled agent visibility bypass (MARVIS_AGENT_VISIBILITY_BYPASS)
# v1.3.0 - 2026-03-13 - Workspace-scoped visibility + cache key (user_id, workspace_id)
from __future__ import annotations

import asyncio
import os
import time
from typing import Final

import aiosqlite
from fastapi import HTTPException

from core.api.models import UserInfo

# H3: agent visibility bypass — controlled via MARVIS_AGENT_VISIBILITY_BYPASS env var.
# Default true for backward compat. Set false once agent tokens have team membership configured.
_AGENT_BYPASS = os.getenv("MARVIS_AGENT_VISIBILITY_BYPASS", "true").lower() == "true"

_cache_lock = asyncio.Lock()
# Cache key: (user_id, workspace_id) → (timestamp, visible_slugs | None)
_visibility_cache: dict[tuple[str, str], tuple[float, set[str] | None]] = {}
_VISIBILITY_TTL: Final = 30  # seconds — reduced from 120s to limit stale-access window


async def get_visible_projects(
    db: aiosqlite.Connection,
    current_user: UserInfo,
    workspace_id: str | None = None,
) -> set[str] | None:
    """Return set of visible project slugs for the user, or None if all projects visible.

    Returns None for admin/super_admin/agent (unrestricted access within workspace).
    Returns set of slugs for operators/viewers (team-filtered within workspace).

    When workspace_id is provided, results are scoped to that workspace.
    """
    if current_user.system_role in ("admin", "super_admin"):
        return None
    # H3: agent visibility bypass — controlled via MARVIS_AGENT_VISIBILITY_BYPASS env var.
    # Default true for backward compat. Set false once agent tokens have team membership configured.
    if getattr(current_user, "user_type", "human") == "agent" and _AGENT_BYPASS:
        return None
    # OSS local single-user (gh #18/#20): the loopback-only Local Operator
    # (security._local_single_user_info, user_id "local") owns the whole brain
    # by design — there is no second user to hide anything from. Without this
    # branch it resolved as an operator with zero teams -> EMPTY visible set ->
    # every task and project filtered out of the local GUI. Real DB users all
    # have usr_* ids, so this cannot match a hosted account.
    if current_user.user_id == "local" and getattr(current_user, "user_type", "human") == "human":
        return None

    ws = workspace_id or current_user.workspace_id or "ws_default"
    cache_key = (current_user.user_id, ws)

    async with _cache_lock:
        cached = _visibility_cache.get(cache_key)
        if cached and time.monotonic() - cached[0] < _VISIBILITY_TTL:
            return cached[1]

    # Query scoped to workspace via teams.workspace_id
    async with db.execute(
        """
        SELECT pt.project FROM project_teams pt
        JOIN team_members tm ON pt.team_id = tm.team_id
        JOIN teams t ON pt.team_id = t.id AND t.deleted_at IS NULL
        WHERE tm.user_id = ?
          AND COALESCE(t.workspace_id, 'ws_default') = ?
        UNION
        SELECT pt2.project FROM project_teams pt2
        JOIN teams t2 ON pt2.team_id = t2.id AND t2.deleted_at IS NULL
        WHERE pt2.is_public = 1
          AND COALESCE(t2.workspace_id, 'ws_default') = ?
        """,
        [current_user.user_id, ws, ws],
    ) as cursor:
        rows = await cursor.fetchall()

    result: set[str] = {r[0] for r in rows}
    async with _cache_lock:
        _visibility_cache[cache_key] = (time.monotonic(), result)
    return result


async def invalidate_visibility_cache_for_team(team_id: str, db: aiosqlite.Connection) -> None:
    """Invalidate cached visibility for all members of the given team."""
    async with db.execute(
        "SELECT user_id FROM team_members WHERE team_id = ?", [team_id]
    ) as cursor:
        members = await cursor.fetchall()
    async with _cache_lock:
        # Remove all cache entries for affected users (any workspace)
        for row in members:
            uid = row[0]
            keys_to_remove = [k for k in _visibility_cache if k[0] == uid]
            for k in keys_to_remove:
                del _visibility_cache[k]


async def invalidate_visibility_cache_for_user(user_id: str) -> None:
    """Invalidate cached visibility for a specific user (all workspaces)."""
    async with _cache_lock:
        keys_to_remove = [k for k in _visibility_cache if k[0] == user_id]
        for k in keys_to_remove:
            del _visibility_cache[k]


async def check_project_access(
    slug: str,
    current_user: UserInfo,
    db: aiosqlite.Connection,
    workspace_id: str | None = None,
) -> None:
    """Raise 404 if project not visible to user (404 not 403 -- does not reveal existence)."""
    visible = await get_visible_projects(db, current_user, workspace_id)
    if visible is not None and slug not in visible:
        raise HTTPException(status_code=404, detail="Not found")


async def filter_visible_edges(
    db: aiosqlite.Connection,
    user: UserInfo,
    edges: list[dict],
) -> tuple[list[dict], int]:
    """Drop edges whose source OR target project is not in user's visible set.

    Returns (filtered_edges, hidden_count).

    Each edge dict must have `source_project` and `target_project` keys.
    Edges missing these keys are kept (conservative: unknown project = visible).

    Admin / agent with bypass active → all edges kept (hidden_count = 0).
    """
    visible = await get_visible_projects(db, user)
    if visible is None:
        # Admin or agent bypass: unrestricted
        return edges, 0

    kept: list[dict] = []
    for e in edges:
        src_proj = e.get("source_project")
        tgt_proj = e.get("target_project")
        # If project info is absent on either side, keep the edge (conservative)
        if (src_proj is None or src_proj in visible) and (
            tgt_proj is None or tgt_proj in visible
        ):
            kept.append(e)

    return kept, len(edges) - len(kept)
