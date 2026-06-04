# v1.6.0 - 2026-03-14 - Fix workspace isolation on PATCH/DELETE/RACI endpoints
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException, Query

from core.api.db import get_db, get_write_db
from core.api.models import UserCreateRequest, UserUpdateRequest, UserResponse
from core.api.models.users import UserTeamSummary
from core.api.rbac import ROLE_HIERARCHY, require_role
from core.api.security import get_current_user, get_current_user_or_agent

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/users", tags=["users"])

_AVATAR_COLORS = [
    "#6366f1",  # indigo (default)
    "#8b5cf6",  # violet
    "#ec4899",  # pink
    "#ef4444",  # red
    "#f97316",  # orange
    "#eab308",  # yellow
    "#22c55e",  # green
    "#06b6d4",  # cyan
]


def _row_to_user(row: aiosqlite.Row) -> UserResponse:
    channels_raw = row["notification_channels"] or "[]"
    try:
        channels = json.loads(channels_raw)
    except (json.JSONDecodeError, TypeError):
        channels = []
    return UserResponse(
        id=row["id"],
        slug=row["slug"],
        display_name=row["display_name"],
        type=row["type"],
        email=row["email"],
        avatar_color=row["avatar_color"] or "#6366f1",
        system_role=row["system_role"],
        notification_channels=channels,
        telegram_chat_id=row["telegram_chat_id"],
        last_used_at=row["last_used_at"],
        deleted_at=row["deleted_at"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        linux_username=row["linux_username"] if "linux_username" in row.keys() else None,
        provisioned_at=row["provisioned_at"] if "provisioned_at" in row.keys() else None,
        onboarding_completed=bool(row["onboarding_completed"]) if "onboarding_completed" in row.keys() else False,
    )


@router.get("", response_model=list[UserResponse])
async def list_users(
    project: str | None = Query(None, description="Filter users by team membership of this project"),
    user=Depends(require_role("admin", "super_admin")),
    db: aiosqlite.Connection = Depends(get_db),
):
    """Lista utenti attivi (deleted_at IS NULL), ordinati per display_name.

    When ?project=slug is provided, returns only users who are members of
    a team assigned to that project (team-scoped filtering).
    """
    db.row_factory = aiosqlite.Row
    ws = user.workspace_id or "ws_default"

    if project:
        # Team-scoped: users who belong to a team assigned to this project (in workspace)
        async with db.execute(
            """
            SELECT DISTINCT u.* FROM users u
            JOIN team_members tm ON u.id = tm.user_id
            JOIN project_teams pt ON tm.team_id = pt.team_id
            JOIN teams t ON tm.team_id = t.id AND t.deleted_at IS NULL
            WHERE pt.project = ? AND u.deleted_at IS NULL
              AND COALESCE(u.workspace_id, 'ws_default') = ?
            ORDER BY u.display_name
            """,
            [project, ws],
        ) as cursor:
            rows = await cursor.fetchall()
    else:
        async with db.execute(
            "SELECT * FROM users WHERE deleted_at IS NULL AND COALESCE(workspace_id, 'ws_default') = ? ORDER BY display_name",
            [ws],
        ) as cursor:
            rows = await cursor.fetchall()

    # Fetch all team memberships in one query for efficiency
    async with db.execute(
        "SELECT tm.user_id, t.id, t.slug, t.display_name, tm.role "
        "FROM team_members tm JOIN teams t ON tm.team_id = t.id "
        "WHERE t.deleted_at IS NULL ORDER BY t.display_name"
    ) as cursor:
        team_rows = await cursor.fetchall()

    # Group teams by user_id
    teams_by_user: dict[str, list[UserTeamSummary]] = {}
    for tr in team_rows:
        uid = tr["user_id"]
        if uid not in teams_by_user:
            teams_by_user[uid] = []
        teams_by_user[uid].append(UserTeamSummary(
            id=tr["id"],
            slug=tr["slug"],
            display_name=tr["display_name"],
            role=tr["role"] or "member",
        ))

    result = []
    for r in rows:
        u = _row_to_user(r)
        u.teams = teams_by_user.get(r["id"], [])
        result.append(u)
    return result


@router.post("", response_model=UserResponse, status_code=201)
async def create_user(
    body: UserCreateRequest,
    caller=Depends(require_role("admin", "super_admin", human_only=True)),
    db: aiosqlite.Connection = Depends(get_write_db),
):
    """Crea un nuovo utente. Slug deve essere unico. Admin+ human-only."""
    # Privilege escalation check: cannot create user with higher role than own
    caller_level = ROLE_HIERARCHY.get(caller.system_role, 0)
    requested_level = ROLE_HIERARCHY.get(body.system_role, 0)
    if requested_level > caller_level:
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    user_id = f"usr_{body.slug}"
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    avatar = body.avatar_color or "#6366f1"
    channels = json.dumps(body.notification_channels)

    try:
        await db.execute(
            "INSERT INTO users "
            "(id, slug, display_name, type, email, avatar_color, system_role, "
            "notification_channels, telegram_chat_id, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                user_id,
                body.slug,
                body.display_name,
                body.type,
                body.email,
                avatar,
                body.system_role,
                channels,
                body.telegram_chat_id,
                now,
                now,
            ),
        )
        await db.commit()
    except Exception as exc:
        if "UNIQUE" in str(exc):
            raise HTTPException(
                status_code=409,
                detail=f"User slug '{body.slug}' already exists",
            )
        raise

    db.row_factory = aiosqlite.Row
    row = await (
        await db.execute("SELECT * FROM users WHERE id = ?", (user_id,))
    ).fetchone()
    return _row_to_user(row)


@router.get("/{user_id}", response_model=UserResponse)
async def get_user(
    user_id: str,
    user=Depends(get_current_user_or_agent),
    db: aiosqlite.Connection = Depends(get_db),
):
    db.row_factory = aiosqlite.Row
    if user.system_role == "super_admin":
        row = await (
            await db.execute("SELECT * FROM users WHERE id = ?", (user_id,))
        ).fetchone()
    else:
        ws = user.workspace_id or "ws_default"
        row = await (
            await db.execute(
                "SELECT * FROM users WHERE id = ? AND COALESCE(workspace_id, 'ws_default') = ?",
                (user_id, ws)
            )
        ).fetchone()
    if not row or row["deleted_at"]:
        raise HTTPException(status_code=404, detail="User not found")
    return _row_to_user(row)


@router.get("/{user_id}/raci", response_model=list[dict])
async def get_user_raci(
    user_id: str,
    user=Depends(get_current_user_or_agent),
    db: aiosqlite.Connection = Depends(get_db),
):
    """Progetti dove l'utente appare nel RACI. Usato dagli agenti per scoped monitoring."""
    db.row_factory = aiosqlite.Row
    if user.system_role != "super_admin":
        ws = user.workspace_id or "ws_default"
        check = await (
            await db.execute(
                "SELECT id FROM users WHERE id = ? AND COALESCE(workspace_id, 'ws_default') = ?",
                (user_id, ws)
            )
        ).fetchone()
        if not check:
            raise HTTPException(status_code=404, detail="User not found")
    async with db.execute(
        "SELECT project, role FROM project_raci WHERE user_id = ? ORDER BY project",
        (user_id,),
    ) as cursor:
        rows = await cursor.fetchall()
    return [{"project": r["project"], "role": r["role"]} for r in rows]


@router.patch("/{user_id}", response_model=UserResponse)
async def update_user(
    user_id: str,
    body: UserUpdateRequest,
    caller=Depends(require_role("admin", "super_admin", human_only=True)),
    db: aiosqlite.Connection = Depends(get_write_db),
):
    """Aggiorna display_name, email, avatar_color, notification_channels, telegram_chat_id."""
    # Guard for future: se system_role viene aggiunto a UserUpdateRequest
    if hasattr(body, "system_role") and body.system_role is not None:
        caller_level = ROLE_HIERARCHY.get(caller.system_role, 0)
        requested_level = ROLE_HIERARCHY.get(body.system_role, 0)
        if requested_level > caller_level:
            raise HTTPException(status_code=403, detail="Insufficient permissions")
    db.row_factory = aiosqlite.Row
    if caller.system_role == "super_admin":
        row = await (
            await db.execute("SELECT * FROM users WHERE id = ?", (user_id,))
        ).fetchone()
    else:
        ws = caller.workspace_id or "ws_default"
        row = await (
            await db.execute(
                "SELECT * FROM users WHERE id = ? AND COALESCE(workspace_id, 'ws_default') = ?",
                (user_id, ws)
            )
        ).fetchone()
    if not row or row["deleted_at"]:
        raise HTTPException(status_code=404, detail="User not found")

    provided = body.model_fields_set
    updates: dict = {}
    if "display_name" in provided and body.display_name is not None:
        updates["display_name"] = body.display_name
    if "email" in provided:
        updates["email"] = body.email
    if "avatar_color" in provided:
        updates["avatar_color"] = body.avatar_color
    if "notification_channels" in provided and body.notification_channels is not None:
        updates["notification_channels"] = json.dumps(body.notification_channels)
    if "telegram_chat_id" in provided:
        updates["telegram_chat_id"] = body.telegram_chat_id
    if "system_role" in provided and body.system_role is not None:
        updates["system_role"] = body.system_role
    if "linux_username" in provided:
        updates["linux_username"] = body.linux_username
    if "provisioned_at" in provided:
        updates["provisioned_at"] = body.provisioned_at
    if "onboarding_completed" in provided and body.onboarding_completed is not None:
        updates["onboarding_completed"] = 1 if body.onboarding_completed else 0

    if not updates:
        return _row_to_user(row)

    updates["updated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    set_clause = ", ".join(f"{k} = ?" for k in updates)
    values = list(updates.values()) + [user_id]
    await db.execute(f"UPDATE users SET {set_clause} WHERE id = ?", values)
    await db.commit()

    row = await (
        await db.execute("SELECT * FROM users WHERE id = ?", (user_id,))
    ).fetchone()
    return _row_to_user(row)


@router.delete("/{user_id}", status_code=204)
async def delete_user(
    user_id: str,
    caller=Depends(require_role("admin", "super_admin", human_only=True)),
    db: aiosqlite.Connection = Depends(get_write_db),
):
    """Soft delete — imposta deleted_at. Le RACI entries rimangono per audit trail."""
    db.row_factory = aiosqlite.Row
    if caller.system_role == "super_admin":
        row = await (
            await db.execute(
                "SELECT id, deleted_at, system_role FROM users WHERE id = ?", (user_id,)
            )
        ).fetchone()
    else:
        ws = caller.workspace_id or "ws_default"
        row = await (
            await db.execute(
                "SELECT id, deleted_at, system_role FROM users WHERE id = ? AND COALESCE(workspace_id, 'ws_default') = ?",
                (user_id, ws)
            )
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="User not found")
    if row["deleted_at"]:
        raise HTTPException(status_code=409, detail="User already deleted")

    # Upward-deletion block: cannot delete user with role >= own role
    target_level = ROLE_HIERARCHY.get(row["system_role"], 0)
    caller_level = ROLE_HIERARCHY.get(caller.system_role, 0)
    if target_level >= caller_level:
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    await db.execute(
        "UPDATE users SET deleted_at = ?, updated_at = ? WHERE id = ?",
        (now, now, user_id),
    )
    await db.commit()
