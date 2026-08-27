"""FastAPI-free team domain authority shared by HTTP and MCP adapters."""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from typing import Any

import aiosqlite

from core.api.services import access_grants
from core.api.services.audit import log_audit
from core.api.use_cases._context import (
    CallerContext,
    require_role_ctx,
    require_workspace_ctx,
)
from core.api.use_cases._errors import (
    AuthorizationError,
    ConflictError,
    NotFoundError,
    ServiceError,
    ValidationError,
)
from core.api.use_cases._roles import ROLE_HIERARCHY


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower().strip()).strip("-")
    return slug[:50]


def _scoped_team_id(workspace_id: str, slug: str) -> str:
    workspace_digest = hashlib.sha256(workspace_id.encode("utf-8")).hexdigest()[:16]
    return f"team_{slug}_{workspace_digest}"


def _team_id(workspace_id: str, slug: str) -> str:
    """Keep legacy OSS ids while making hosted/enterprise ids tenant-safe."""
    if workspace_id == "ws_default":
        return f"team_{slug}"
    return _scoped_team_id(workspace_id, slug)


async def _allocate_team_id(
    db: aiosqlite.Connection,
    workspace_id: str,
    slug: str,
) -> str:
    """Choose a stable id while preserving foreign pre-workspace legacy rows."""
    preferred_id = _team_id(workspace_id, slug)
    async with db.execute(
        "SELECT 1 FROM teams WHERE id = ?",
        (preferred_id,),
    ) as cursor:
        preferred_taken = await cursor.fetchone() is not None
    if not preferred_taken:
        return preferred_id

    fallback_id = _scoped_team_id(workspace_id, slug)
    if fallback_id != preferred_id:
        async with db.execute(
            "SELECT 1 FROM teams WHERE id = ?",
            (fallback_id,),
        ) as cursor:
            fallback_taken = await cursor.fetchone() is not None
        if not fallback_taken:
            return fallback_id

    raise ConflictError(
        code="team_exists",
        message=f"Team slug '{slug}' already exists",
    )


def _is_global_admin(ctx: CallerContext) -> bool:
    return ctx.system_role in ("admin", "super_admin")


def _require_active_lead(ctx: CallerContext) -> None:
    if ROLE_HIERARCHY.get(ctx.system_role, -1) < ROLE_HIERARCHY["operator"]:
        raise AuthorizationError(
            code="insufficient_permissions",
            message="Insufficient permissions",
        )


def _creator_is_person(ctx: CallerContext) -> bool:
    return ctx.user_type == "human" and bool(ctx.user_id) and ctx.user_id != "local"


async def _get_team(
    db: aiosqlite.Connection,
    team: str,
    workspace_id: str,
) -> aiosqlite.Row:
    async with db.execute(
        "SELECT id,slug,display_name,description,avatar_color,created_at "
        "FROM teams WHERE workspace_id = ? AND (id = ? OR slug = ?) "
        "AND deleted_at IS NULL",
        (workspace_id, team, team),
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        raise NotFoundError(code="team_not_found", message="Team not found")
    return row


async def _check_team_admin(
    db: aiosqlite.Connection,
    ctx: CallerContext,
    team_id: str,
) -> bool:
    if _is_global_admin(ctx):
        return True
    async with db.execute(
        "SELECT 1 FROM team_members "
        "WHERE team_id = ? AND user_id = ? AND role = 'admin'",
        (team_id, ctx.user_id),
    ) as cursor:
        return await cursor.fetchone() is not None


async def _require_team_admin(
    db: aiosqlite.Connection,
    ctx: CallerContext,
    team_id: str,
) -> None:
    if not await _check_team_admin(db, ctx, team_id):
        raise AuthorizationError(
            code="insufficient_permissions",
            message="Insufficient permissions",
        )
    if not _is_global_admin(ctx):
        _require_active_lead(ctx)


async def _resolve_user(
    db: aiosqlite.Connection,
    user: str,
    workspace_id: str,
) -> aiosqlite.Row:
    value = (user or "").strip()
    async with db.execute('PRAGMA table_info("users")') as cursor:
        user_columns = {str(row[1]) for row in await cursor.fetchall()}
    identity_columns = [
        column for column in ("id", "slug", "email") if column in user_columns
    ]
    identity_predicate = " OR ".join(f"{column} = ?" for column in identity_columns)
    async with db.execute(
        "SELECT id,slug,display_name,system_role FROM users "
        "WHERE workspace_id = ? AND deleted_at IS NULL "
        f"AND ({identity_predicate}) LIMIT 1",
        (workspace_id, *(value for _column in identity_columns)),
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        raise NotFoundError(
            code="user_not_found",
            message="User is not a tenant member",
        )
    return row


async def _counts(
    db: aiosqlite.Connection,
    team_id: str,
) -> tuple[int, int]:
    async with db.execute(
        "SELECT COUNT(*) FROM team_members WHERE team_id = ?",
        (team_id,),
    ) as cursor:
        members = (await cursor.fetchone())[0]
    async with db.execute(
        "SELECT COUNT(*) FROM project_teams WHERE team_id = ?",
        (team_id,),
    ) as cursor:
        projects = (await cursor.fetchone())[0]
    return members, projects


def _row_payload(
    row: aiosqlite.Row,
    *,
    member_count: int,
    project_count: int,
    your_role: str | None = None,
) -> dict[str, Any]:
    return {
        "id": row["id"],
        "slug": row["slug"],
        "display_name": row["display_name"],
        "description": row["description"],
        "avatar_color": row["avatar_color"],
        "created_at": row["created_at"],
        "member_count": member_count,
        "project_count": project_count,
        "your_role": your_role,
    }


async def _impacted_projects(
    db: aiosqlite.Connection,
    team_id: str,
) -> list[str]:
    async with db.execute(
        "SELECT project FROM project_teams WHERE team_id = ? ORDER BY project",
        (team_id,),
    ) as cursor:
        return [str(row["project"]) for row in await cursor.fetchall()]


async def create_team(
    db: aiosqlite.Connection,
    ctx: CallerContext,
    *,
    slug: str,
    display_name: str,
    description: str | None,
    avatar_color: str | None = None,
) -> dict[str, Any]:
    require_role_ctx(ctx, "operator", "admin", "super_admin")
    workspace_id = require_workspace_ctx(ctx)
    team_slug = _slugify(slug or display_name)
    if not team_slug:
        raise ValidationError(
            code="invalid_slug",
            message="Cannot derive a valid team slug",
        )
    created_at = _now()
    creator_is_person = _creator_is_person(ctx)
    if db.in_transaction:
        raise RuntimeError("create_team requires a clean transaction boundary")
    await db.execute("BEGIN IMMEDIATE")
    try:
        team_id = await _allocate_team_id(db, workspace_id, team_slug)
        await db.execute(
            "INSERT INTO teams "
            "(id,slug,display_name,description,avatar_color,workspace_id,created_by,created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (
                team_id,
                team_slug,
                display_name,
                description,
                avatar_color,
                workspace_id,
                ctx.user_id if creator_is_person else None,
                created_at,
            ),
        )
        member_count = 0
        if creator_is_person:
            await db.execute(
                "INSERT INTO team_members(team_id,user_id,role,is_admin,joined_at) "
                "VALUES (?,?,'admin',1,?)",
                (team_id, ctx.user_id, created_at),
            )
            member_count = 1
    except aiosqlite.IntegrityError as exc:
        await db.rollback()
        if "UNIQUE" in str(exc):
            raise ConflictError(
                code="team_exists",
                message=f"Team slug '{team_slug}' already exists",
            ) from exc
        raise
    except Exception:
        await db.rollback()
        raise
    try:
        await log_audit(
            db,
            "team.create",
            ctx.user_id or ctx.username,
            "team",
            team_id,
            {"slug": team_slug, "creator_lead": creator_is_person},
            workspace_id=workspace_id,
        )
        await db.commit()
    except Exception:
        await db.rollback()
        raise
    return {
        "id": team_id,
        "slug": team_slug,
        "display_name": display_name,
        "description": description,
        "avatar_color": avatar_color,
        "created_at": created_at,
        "member_count": member_count,
        "project_count": 0,
        "your_role": "admin" if creator_is_person else None,
    }


async def update_team(
    db: aiosqlite.Connection,
    ctx: CallerContext,
    *,
    team: str,
    display_name: str | None,
    description: str | None,
    avatar_color: str | None,
    supplied_fields: set[str],
) -> dict[str, Any]:
    workspace_id = require_workspace_ctx(ctx)
    row = await _get_team(db, team, workspace_id)
    team_id = str(row["id"])
    await _require_team_admin(db, ctx, team_id)
    updates: dict[str, str | None] = {}
    for name, value in (
        ("display_name", display_name),
        ("description", description),
        ("avatar_color", avatar_color),
    ):
        if name in supplied_fields:
            updates[name] = value
    if updates:
        assignments = ", ".join(f"{name} = ?" for name in updates)
        await db.execute(
            f"UPDATE teams SET {assignments} WHERE id = ? AND workspace_id = ?",
            (*updates.values(), team_id, workspace_id),
        )
        await log_audit(
            db,
            "team.update",
            ctx.user_id or ctx.username,
            "team",
            team_id,
            {"fields": sorted(updates)},
            workspace_id=workspace_id,
        )
        await db.commit()
    refreshed = await _get_team(db, team_id, workspace_id)
    members, projects = await _counts(db, team_id)
    return _row_payload(
        refreshed,
        member_count=members,
        project_count=projects,
    )


async def delete_team(
    db: aiosqlite.Connection,
    ctx: CallerContext,
    *,
    team: str,
) -> dict[str, Any]:
    require_role_ctx(ctx, "super_admin")
    if ctx.user_type != "human":
        raise AuthorizationError(
            code="human_required",
            message="Human authentication required",
        )
    workspace_id = require_workspace_ctx(ctx)
    row = await _get_team(db, team, workspace_id)
    team_id = str(row["id"])
    projects = await _impacted_projects(db, team_id)
    async with db.execute(
        "SELECT user_id FROM team_members WHERE team_id = ?",
        (team_id,),
    ) as cursor:
        affected_users = [str(item["user_id"]) for item in await cursor.fetchall()]
    await db.execute(
        "UPDATE teams SET deleted_at = ? WHERE id = ? AND workspace_id = ?",
        (_now(), team_id, workspace_id),
    )
    await log_audit(
        db,
        "team.delete",
        ctx.user_id or ctx.username,
        "team",
        team_id,
        {"impacted_projects": projects},
        workspace_id=workspace_id,
    )
    await db.commit()
    return {"team_id": team_id, "affected_user_ids": affected_users}


async def list_teams(
    db: aiosqlite.Connection,
    ctx: CallerContext,
) -> list[dict[str, Any]]:
    workspace_id = require_workspace_ctx(ctx)
    if _is_global_admin(ctx):
        query = (
            "SELECT t.id,t.slug,t.display_name,t.description,t.avatar_color,t.created_at,"
            "tm.role AS your_role FROM teams t "
            "LEFT JOIN team_members tm ON t.id=tm.team_id AND tm.user_id=? "
            "WHERE t.deleted_at IS NULL AND t.workspace_id=? ORDER BY t.display_name"
        )
    else:
        query = (
            "SELECT t.id,t.slug,t.display_name,t.description,t.avatar_color,t.created_at,"
            "tm.role AS your_role FROM teams t "
            "JOIN team_members tm ON t.id=tm.team_id AND tm.user_id=? "
            "WHERE t.deleted_at IS NULL AND t.workspace_id=? ORDER BY t.display_name"
        )
    async with db.execute(query, (ctx.user_id, workspace_id)) as cursor:
        rows = await cursor.fetchall()
    result: list[dict[str, Any]] = []
    for row in rows:
        members, projects = await _counts(db, str(row["id"]))
        result.append(
            _row_payload(
                row,
                member_count=members,
                project_count=projects,
                your_role=row["your_role"],
            )
        )
    return result


async def list_team_members(
    db: aiosqlite.Connection,
    ctx: CallerContext,
    *,
    team: str,
) -> list[dict[str, Any]]:
    workspace_id = require_workspace_ctx(ctx)
    row = await _get_team(db, team, workspace_id)
    team_id = str(row["id"])
    if not _is_global_admin(ctx):
        async with db.execute(
            "SELECT 1 FROM team_members WHERE team_id=? AND user_id=?",
            (team_id, ctx.user_id),
        ) as cursor:
            if await cursor.fetchone() is None:
                raise AuthorizationError(
                    code="not_team_member",
                    message="Not a member of this team",
                )
    async with db.execute(
        "SELECT tm.user_id,tm.role,tm.joined_at,u.display_name,u.system_role "
        "FROM team_members tm JOIN users u ON u.id=tm.user_id "
        "WHERE tm.team_id=? AND u.workspace_id=? AND u.deleted_at IS NULL "
        "ORDER BY tm.role DESC,u.display_name",
        (team_id, workspace_id),
    ) as cursor:
        return [dict(item) for item in await cursor.fetchall()]


async def add_team_member(
    db: aiosqlite.Connection,
    ctx: CallerContext,
    *,
    team: str,
    user: str,
    role: str,
) -> dict[str, Any]:
    workspace_id = require_workspace_ctx(ctx)
    team_row = await _get_team(db, team, workspace_id)
    team_id = str(team_row["id"])
    await _require_team_admin(db, ctx, team_id)
    if not _is_global_admin(ctx) and role == "admin":
        raise AuthorizationError(
            code="lead_cannot_assign_admin",
            message="Only global admins can assign team admin role",
        )
    target = await _resolve_user(db, user, workspace_id)
    if ROLE_HIERARCHY.get(target["system_role"], 0) > ROLE_HIERARCHY.get(
        ctx.system_role, 0
    ):
        raise AuthorizationError(
            code="target_role_exceeds_caller",
            message="Cannot add user with higher role than your own",
        )
    joined_at = _now()
    try:
        await db.execute(
            "INSERT INTO team_members(team_id,user_id,role,is_admin,joined_at) "
            "VALUES (?,?,?,?,?)",
            (team_id, target["id"], role, int(role == "admin"), joined_at),
        )
    except aiosqlite.IntegrityError as exc:
        if "UNIQUE" not in str(exc):
            raise
        await db.execute(
            "UPDATE team_members SET role=?,is_admin=? "
            "WHERE team_id=? AND user_id=?",
            (role, int(role == "admin"), team_id, target["id"]),
        )
    await log_audit(
        db,
        "team.member_add",
        ctx.user_id or ctx.username,
        "team",
        team_id,
        {
            "member": target["id"],
            "role": role,
            "impacted_projects": await _impacted_projects(db, team_id),
        },
        workspace_id=workspace_id,
    )
    await db.commit()
    return {
        "status": "ok",
        "team_id": team_id,
        "user_id": target["id"],
        "role": role,
    }


async def remove_team_member(
    db: aiosqlite.Connection,
    ctx: CallerContext,
    *,
    team: str,
    user: str,
) -> dict[str, Any]:
    workspace_id = require_workspace_ctx(ctx)
    team_row = await _get_team(db, team, workspace_id)
    team_id = str(team_row["id"])
    await _require_team_admin(db, ctx, team_id)
    target = await _resolve_user(db, user, workspace_id)
    async with db.execute(
        "SELECT role FROM team_members WHERE team_id=? AND user_id=?",
        (team_id, target["id"]),
    ) as cursor:
        membership = await cursor.fetchone()
    if membership is None:
        raise NotFoundError(
            code="not_a_member",
            message="User is not a member of this team",
        )
    if (
        target["id"] == ctx.user_id
        and membership["role"] == "admin"
        and not _is_global_admin(ctx)
    ):
        async with db.execute(
            "SELECT COUNT(*) FROM team_members WHERE team_id=? AND role='admin'",
            (team_id,),
        ) as cursor:
            lead_count = (await cursor.fetchone())[0]
        if lead_count <= 1:
            raise ConflictError(
                code="sole_lead_self_removal",
                message="Sole team lead cannot remove itself — ask an admin to hand the team over first",
            )
    projects = await _impacted_projects(db, team_id)
    await db.execute(
        "DELETE FROM team_members WHERE team_id=? AND user_id=?",
        (team_id, target["id"]),
    )
    await log_audit(
        db,
        "team.member_remove",
        ctx.user_id or ctx.username,
        "team",
        team_id,
        {"member": target["id"], "impacted_projects": projects},
        workspace_id=workspace_id,
    )
    await db.commit()
    return {
        "status": "ok",
        "team_id": team_id,
        "user_id": target["id"],
        "removed": True,
    }


async def list_team_projects(
    db: aiosqlite.Connection,
    ctx: CallerContext,
    *,
    team: str,
) -> list[dict[str, Any]]:
    workspace_id = require_workspace_ctx(ctx)
    row = await _get_team(db, team, workspace_id)
    team_id = str(row["id"])
    if not _is_global_admin(ctx):
        async with db.execute(
            "SELECT 1 FROM team_members WHERE team_id=? AND user_id=?",
            (team_id, ctx.user_id),
        ) as cursor:
            if await cursor.fetchone() is None:
                raise AuthorizationError(
                    code="not_team_member",
                    message="Not a member of this team",
                )
    async with db.execute(
        "SELECT project,is_public,assigned_at,role,clearance "
        "FROM project_teams WHERE team_id=? ORDER BY project",
        (team_id,),
    ) as cursor:
        return [
            {
                "project": item["project"],
                "is_public": bool(item["is_public"]),
                "assigned_at": item["assigned_at"],
                "role": item["role"] or "member",
                "clearance": item["clearance"] or "internal",
            }
            for item in await cursor.fetchall()
        ]


async def assign_team_project(
    db: aiosqlite.Connection,
    ctx: CallerContext,
    *,
    team: str,
    project: str,
    role: str,
    clearance: str,
    is_public: bool | None = None,
) -> dict[str, Any]:
    workspace_id = require_workspace_ctx(ctx)
    await access_grants.require_workspace_project_bound(db, ctx, project)
    team_row = await _get_team(db, team, workspace_id)
    team_id = str(team_row["id"])
    if not await access_grants.is_project_grant_admin(db, ctx, project):
        raise AuthorizationError(
            code="insufficient_permissions",
            message="Insufficient permissions",
        )
    if not _is_global_admin(ctx):
        _require_active_lead(ctx)
    assigned_at = _now()
    action = "team.project_assign"
    try:
        await db.execute(
            "INSERT INTO project_teams"
            "(project,team_id,is_public,role,clearance,assigned_at) "
            "VALUES (?,?,?,?,?,?)",
            (
                project,
                team_id,
                int(is_public) if is_public is not None else 0,
                role,
                clearance,
                assigned_at,
            ),
        )
    except aiosqlite.IntegrityError as exc:
        if "UNIQUE" not in str(exc):
            raise
        action = "team.project_update"
        if is_public is None:
            await db.execute(
                "UPDATE project_teams SET role=?,clearance=? "
                "WHERE project=? AND team_id=?",
                (role, clearance, project, team_id),
            )
        else:
            await db.execute(
                "UPDATE project_teams SET is_public=?,role=?,clearance=? "
                "WHERE project=? AND team_id=?",
                (int(is_public), role, clearance, project, team_id),
            )
    await log_audit(
        db,
        action,
        ctx.user_id or ctx.username,
        "team",
        team_id,
        {"project": project, "role": role, "clearance": clearance},
        workspace_id=workspace_id,
    )
    await db.commit()
    return {
        "status": "ok",
        "project": project,
        "team_id": team_id,
        "role": role,
        "clearance": clearance,
    }


async def unassign_team_project(
    db: aiosqlite.Connection,
    ctx: CallerContext,
    *,
    team: str,
    project: str,
) -> dict[str, Any]:
    workspace_id = require_workspace_ctx(ctx)
    await access_grants.require_workspace_project_bound(db, ctx, project)
    team_row = await _get_team(db, team, workspace_id)
    team_id = str(team_row["id"])
    if not await access_grants.is_project_grant_admin(db, ctx, project):
        raise AuthorizationError(
            code="insufficient_permissions",
            message="Insufficient permissions",
        )
    if not _is_global_admin(ctx):
        _require_active_lead(ctx)
    async with db.execute(
        "SELECT 1 FROM project_teams WHERE project=? AND team_id=?",
        (project, team_id),
    ) as cursor:
        if await cursor.fetchone() is None:
            raise NotFoundError(
                code="project_not_assigned",
                message="Project not assigned to this team",
            )
    await db.execute(
        "DELETE FROM project_teams WHERE project=? AND team_id=?",
        (project, team_id),
    )
    await log_audit(
        db,
        "team.project_unassign",
        ctx.user_id or ctx.username,
        "team",
        team_id,
        {"project": project},
        workspace_id=workspace_id,
    )
    await db.commit()
    return {
        "status": "ok",
        "project": project,
        "team_id": team_id,
        "removed": True,
    }


async def set_user_role(
    db: aiosqlite.Connection,
    ctx: CallerContext,
    *,
    user: str,
    role: str,
) -> dict[str, Any]:
    require_role_ctx(ctx, "admin", "super_admin")
    workspace_id = require_workspace_ctx(ctx)
    if role not in ("viewer", "operator", "admin"):
        raise AuthorizationError(
            code="role_not_assignable",
            message="Only viewer/operator/admin are assignable — super_admin is manual-only",
        )
    target = await _resolve_user(db, user, workspace_id)
    old_role = str(target["system_role"] or "viewer")
    if old_role == "super_admin":
        raise AuthorizationError(
            code="super_admin_immutable",
            message="super_admin roles are managed manually — refusing to modify",
        )
    if old_role == role:
        return {
            "user_id": target["id"],
            "slug": target["slug"],
            "old_role": old_role,
            "new_role": role,
            "changed": False,
        }
    if old_role == "admin" and ROLE_HIERARCHY[role] < ROLE_HIERARCHY["admin"]:
        async with db.execute(
            "SELECT COUNT(*) FROM users WHERE deleted_at IS NULL "
            "AND workspace_id=? AND system_role IN ('admin','super_admin') AND id!=?",
            (workspace_id, target["id"]),
        ) as cursor:
            remaining = (await cursor.fetchone())[0]
        if remaining == 0:
            raise ConflictError(
                code="last_admin",
                message="Refusing the change: it would leave the tenant with no admins",
            )
    await db.execute(
        "UPDATE users SET system_role=?,"
        "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') "
        "WHERE id=? AND workspace_id=?",
        (role, target["id"], workspace_id),
    )
    await log_audit(
        db,
        "users.role_change",
        ctx.user_id or ctx.username,
        "user",
        target["id"],
        {"target": target["id"], "old": old_role, "new": role},
        workspace_id=workspace_id,
    )
    await db.commit()
    return {
        "user_id": target["id"],
        "slug": target["slug"],
        "old_role": old_role,
        "new_role": role,
        "changed": True,
    }


# Backwards-compatible names used by focused tests and older internal callers.
create_team_impl = create_team
list_teams_impl = list_teams
add_team_member_impl = add_team_member
remove_team_member_impl = remove_team_member
assign_team_project_impl = assign_team_project
unassign_team_project_impl = unassign_team_project
set_user_role_impl = set_user_role


__all__ = [
    "add_team_member",
    "assign_team_project",
    "create_team",
    "delete_team",
    "list_team_members",
    "list_team_projects",
    "list_teams",
    "remove_team_member",
    "set_user_role",
    "unassign_team_project",
    "update_team",
]
