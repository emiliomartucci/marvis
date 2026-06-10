# v3.0.0 - 2026-03-09 - Flat teams: role column, team admin can edit own team, avatar_color
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException

from core.api.db import get_db, get_write_db
from core.api.models import UserInfo
from core.api.models.teams import (
    TeamCreateRequest,
    TeamMemberRequest,
    TeamMemberResponse,
    TeamProjectAssignRequest,
    TeamProjectResponse,
    TeamResponse,
    TeamUpdateRequest,
)
from core.api.rbac import ROLE_HIERARCHY, check_team_admin, require_role
from core.api.security import get_current_user, get_current_user_or_agent
from core.api.visibility import invalidate_visibility_cache_for_team

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/teams", tags=["teams"])


def _slugify(text: str) -> str:
    """Simple slugify: lowercase, replace non-alphanumeric with hyphens, strip edges."""
    slug = text.lower().strip()
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    slug = slug.strip("-")
    return slug[:50]


async def _get_team_or_404(team_id: str, db: aiosqlite.Connection) -> aiosqlite.Row:
    """Fetch team row or raise 404."""
    async with db.execute(
        "SELECT id, slug, display_name, description, avatar_color, created_at FROM teams "
        "WHERE id = ? AND deleted_at IS NULL",
        [team_id],
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Team not found")
    return row


async def _team_response(row: aiosqlite.Row, db: aiosqlite.Connection) -> TeamResponse:
    """Build TeamResponse from a team row, including counts."""
    async with db.execute(
        "SELECT COUNT(*) FROM team_members WHERE team_id = ?", [row["id"]]
    ) as mc:
        mem_count = (await mc.fetchone())[0]
    async with db.execute(
        "SELECT COUNT(*) FROM project_teams WHERE team_id = ?", [row["id"]]
    ) as pc:
        proj_count = (await pc.fetchone())[0]
    return TeamResponse(
        id=row["id"],
        slug=row["slug"],
        display_name=row["display_name"],
        description=row["description"],
        avatar_color=row["avatar_color"],
        created_at=row["created_at"],
        member_count=mem_count,
        project_count=proj_count,
    )


@router.post("", response_model=TeamResponse, status_code=201)
async def create_team(
    body: TeamCreateRequest,
    current_user: UserInfo = Depends(require_role("admin", "super_admin")),
    db: aiosqlite.Connection = Depends(get_write_db),
) -> TeamResponse:
    """Create a new team. Admin+ only."""
    slug = body.slug or _slugify(body.display_name)
    if not slug:
        raise HTTPException(status_code=422, detail="Cannot derive a valid slug from display_name")
    team_id = f"team_{slug}"
    now = datetime.now(timezone.utc).isoformat()

    try:
        ws = current_user.workspace_id or "ws_default"
        await db.execute(
            "INSERT INTO teams (id, slug, display_name, description, avatar_color, workspace_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [team_id, slug, body.display_name, body.description, body.avatar_color, ws, now],
        )
        await db.commit()
    except Exception as exc:
        if "UNIQUE" in str(exc):
            raise HTTPException(status_code=409, detail=f"Team slug '{slug}' already exists")
        raise

    return TeamResponse(
        id=team_id,
        slug=slug,
        display_name=body.display_name,
        description=body.description,
        avatar_color=body.avatar_color,
        created_at=now,
        member_count=0,
        project_count=0,
    )


@router.patch("/{team_id}", response_model=TeamResponse)
async def update_team(
    team_id: str,
    body: TeamUpdateRequest,
    current_user: UserInfo = Depends(get_current_user),
    db: aiosqlite.Connection = Depends(get_write_db),
) -> TeamResponse:
    """Update a team's display_name, description, or avatar_color.

    Allowed for: global admin/super_admin OR team admin of this team.
    """
    await _get_team_or_404(team_id, db)

    # Team admin or global admin can edit
    if not await check_team_admin(team_id, current_user, db):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    updates: dict[str, str | None] = {}
    if body.display_name is not None:
        updates["display_name"] = body.display_name
    if body.description is not None:
        updates["description"] = body.description
    if body.avatar_color is not None:
        updates["avatar_color"] = body.avatar_color

    if updates:
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [team_id]
        await db.execute(f"UPDATE teams SET {set_clause} WHERE id = ?", values)
        await db.commit()

    # Refetch to return updated row
    row = await _get_team_or_404(team_id, db)
    return await _team_response(row, db)


@router.delete("/{team_id}", status_code=204)
async def delete_team(
    team_id: str,
    current_user: UserInfo = Depends(require_role("super_admin", human_only=True)),
    db: aiosqlite.Connection = Depends(get_write_db),
):
    """Soft-delete a team. Super_admin human-only (team admins cannot delete)."""
    await _get_team_or_404(team_id, db)

    # CRITICAL: invalidate visibility cache BEFORE soft delete
    await invalidate_visibility_cache_for_team(team_id, db)

    now = datetime.now(timezone.utc).isoformat()
    await db.execute("UPDATE teams SET deleted_at = ? WHERE id = ?", [now, team_id])
    await db.commit()


@router.get("", response_model=list[TeamResponse])
async def list_teams(
    current_user: UserInfo = Depends(get_current_user_or_agent),
    db: aiosqlite.Connection = Depends(get_db),
) -> list[TeamResponse]:
    """List teams visible to the current user."""
    ws = current_user.workspace_id or "ws_default"
    if current_user.system_role in ("admin", "super_admin"):
        # Admins see all teams in their workspace
        async with db.execute(
            "SELECT id, slug, display_name, description, avatar_color, created_at FROM teams "
            "WHERE deleted_at IS NULL AND COALESCE(workspace_id, 'ws_default') = ? ORDER BY display_name",
            [ws],
        ) as cursor:
            rows = await cursor.fetchall()
    else:
        # Regular users: teams they are members of (in their workspace)
        async with db.execute(
            "SELECT t.id, t.slug, t.display_name, t.description, t.avatar_color, t.created_at "
            "FROM teams t JOIN team_members tm ON t.id = tm.team_id "
            "WHERE tm.user_id = ? AND t.deleted_at IS NULL AND COALESCE(t.workspace_id, 'ws_default') = ? "
            "ORDER BY t.display_name",
            [current_user.user_id, ws],
        ) as cursor:
            rows = await cursor.fetchall()

    result = []
    for row in rows:
        result.append(await _team_response(row, db))
    return result


@router.get("/{team_id}/members", response_model=list[TeamMemberResponse])
async def list_team_members(
    team_id: str,
    current_user: UserInfo = Depends(get_current_user_or_agent),
    db: aiosqlite.Connection = Depends(get_db),
) -> list[TeamMemberResponse]:
    """List members of a team."""
    await _get_team_or_404(team_id, db)

    # Must be a member or admin to list members
    if current_user.system_role not in ("admin", "super_admin"):
        async with db.execute(
            "SELECT 1 FROM team_members WHERE team_id = ? AND user_id = ?",
            [team_id, current_user.user_id],
        ) as cursor:
            if await cursor.fetchone() is None:
                raise HTTPException(status_code=403, detail="Not a member of this team")

    async with db.execute(
        "SELECT tm.user_id, tm.role, tm.joined_at, u.display_name, u.system_role "
        "FROM team_members tm JOIN users u ON tm.user_id = u.id "
        "WHERE tm.team_id = ? AND u.deleted_at IS NULL "
        "ORDER BY tm.role DESC, u.display_name",
        [team_id],
    ) as cursor:
        rows = await cursor.fetchall()

    return [
        TeamMemberResponse(
            user_id=r["user_id"],
            display_name=r["display_name"],
            system_role=r["system_role"],
            role=r["role"],
            joined_at=r["joined_at"],
        )
        for r in rows
    ]


@router.post("/{team_id}/members", status_code=201)
async def add_team_member(
    team_id: str,
    body: TeamMemberRequest,
    current_user: UserInfo = Depends(get_current_user),
    db: aiosqlite.Connection = Depends(get_write_db),
):
    """Add a member to a team. Requires team_admin or admin+."""
    await _get_team_or_404(team_id, db)

    # Must be team admin or global admin
    if not await check_team_admin(team_id, current_user, db):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    # Only global admin can assign team admin role
    if body.role == "admin" and current_user.system_role not in ("admin", "super_admin"):
        # Team admins can only add members, not other admins
        is_caller_team_admin = await check_team_admin(team_id, current_user, db)
        if is_caller_team_admin and current_user.system_role not in ("admin", "super_admin"):
            raise HTTPException(
                status_code=403,
                detail="Only global admins can assign team admin role",
            )

    # Fetch target user
    async with db.execute(
        "SELECT id, system_role FROM users WHERE id = ? AND deleted_at IS NULL",
        [body.user_id],
    ) as cursor:
        target_row = await cursor.fetchone()
    if target_row is None:
        raise HTTPException(status_code=404, detail="User not found")

    # Privilege escalation check: team_admin cannot add users with higher role than themselves
    caller_level = ROLE_HIERARCHY.get(current_user.system_role, 0)
    target_level = ROLE_HIERARCHY.get(target_row["system_role"], 0)
    if target_level > caller_level:
        raise HTTPException(
            status_code=403,
            detail="Cannot add user with higher role than your own",
        )

    now = datetime.now(timezone.utc).isoformat()
    try:
        await db.execute(
            "INSERT INTO team_members (team_id, user_id, role, is_admin, joined_at) "
            "VALUES (?, ?, ?, ?, ?)",
            [team_id, body.user_id, body.role, int(body.role == "admin"), now],
        )
        await db.commit()
    except Exception as exc:
        if "UNIQUE" in str(exc):
            # Already a member -- update role if needed
            await db.execute(
                "UPDATE team_members SET role = ?, is_admin = ? "
                "WHERE team_id = ? AND user_id = ?",
                [body.role, int(body.role == "admin"), team_id, body.user_id],
            )
            await db.commit()
        else:
            raise

    await invalidate_visibility_cache_for_team(team_id, db)
    return {"status": "ok", "team_id": team_id, "user_id": body.user_id, "role": body.role}


@router.delete("/{team_id}/members/{user_id}", status_code=204)
async def remove_team_member(
    team_id: str,
    user_id: str,
    current_user: UserInfo = Depends(get_current_user),
    db: aiosqlite.Connection = Depends(get_write_db),
):
    """Remove a member from a team. Requires team_admin or admin+."""
    await _get_team_or_404(team_id, db)

    if not await check_team_admin(team_id, current_user, db):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    async with db.execute(
        "SELECT 1 FROM team_members WHERE team_id = ? AND user_id = ?",
        [team_id, user_id],
    ) as cursor:
        if await cursor.fetchone() is None:
            raise HTTPException(status_code=404, detail="User is not a member of this team")

    await db.execute(
        "DELETE FROM team_members WHERE team_id = ? AND user_id = ?",
        [team_id, user_id],
    )
    await db.commit()
    await invalidate_visibility_cache_for_team(team_id, db)


@router.get("/{team_id}/projects", response_model=list[TeamProjectResponse])
async def list_team_projects(
    team_id: str,
    current_user: UserInfo = Depends(get_current_user_or_agent),
    db: aiosqlite.Connection = Depends(get_db),
) -> list[TeamProjectResponse]:
    """List projects assigned to a team."""
    await _get_team_or_404(team_id, db)

    # Must be a member or admin to list projects
    if current_user.system_role not in ("admin", "super_admin"):
        async with db.execute(
            "SELECT 1 FROM team_members WHERE team_id = ? AND user_id = ?",
            [team_id, current_user.user_id],
        ) as cursor:
            if await cursor.fetchone() is None:
                raise HTTPException(status_code=403, detail="Not a member of this team")

    async with db.execute(
        "SELECT project, is_public, assigned_at FROM project_teams "
        "WHERE team_id = ? ORDER BY project",
        [team_id],
    ) as cursor:
        rows = await cursor.fetchall()

    return [
        TeamProjectResponse(
            project=r["project"],
            is_public=bool(r["is_public"]),
            assigned_at=r["assigned_at"],
        )
        for r in rows
    ]


@router.post("/{team_id}/projects", status_code=201)
async def assign_team_project(
    team_id: str,
    body: TeamProjectAssignRequest,
    current_user: UserInfo = Depends(get_current_user),
    db: aiosqlite.Connection = Depends(get_write_db),
):
    """Assign a project to a team. Requires admin+."""
    await _get_team_or_404(team_id, db)

    # Only global admin can assign projects to teams
    if ROLE_HIERARCHY.get(current_user.system_role, 0) < ROLE_HIERARCHY["admin"]:
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    now = datetime.now(timezone.utc).isoformat()
    try:
        await db.execute(
            "INSERT INTO project_teams (project, team_id, is_public, assigned_at) VALUES (?, ?, ?, ?)",
            [body.project, team_id, int(body.is_public), now],
        )
        await db.commit()
    except Exception as exc:
        if "UNIQUE" in str(exc):
            await db.execute(
                "UPDATE project_teams SET is_public = ? WHERE project = ? AND team_id = ?",
                [int(body.is_public), body.project, team_id],
            )
            await db.commit()
        else:
            raise

    await invalidate_visibility_cache_for_team(team_id, db)
    return {"status": "ok", "project": body.project, "team_id": team_id}


@router.delete("/{team_id}/projects/{slug}", status_code=204)
async def remove_team_project(
    team_id: str,
    slug: str,
    current_user: UserInfo = Depends(get_current_user),
    db: aiosqlite.Connection = Depends(get_write_db),
):
    """Remove a project from a team. Requires admin+."""
    await _get_team_or_404(team_id, db)

    if ROLE_HIERARCHY.get(current_user.system_role, 0) < ROLE_HIERARCHY["admin"]:
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    async with db.execute(
        "SELECT 1 FROM project_teams WHERE project = ? AND team_id = ?",
        [slug, team_id],
    ) as cursor:
        if await cursor.fetchone() is None:
            raise HTTPException(status_code=404, detail="Project not assigned to this team")

    await db.execute(
        "DELETE FROM project_teams WHERE project = ? AND team_id = ?",
        [slug, team_id],
    )
    await db.commit()
    await invalidate_visibility_cache_for_team(team_id, db)
