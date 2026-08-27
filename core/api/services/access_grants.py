"""Tenant access grants and read/write predicates.

Sprint 1 hosted-tenant-first security floor:
``can_read = project/scope access AND clearance(resource) <= clearance(actor)``.

This module is FastAPI-free so HTTP routers, use-cases, and MCP tools share one
predicate instead of drifting into per-surface visibility rules.
"""
from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

import aiosqlite

from core.api.config import settings
from core.api.use_cases._context import CallerContext, require_workspace_ctx
from core.api.use_cases._errors import NotFoundError, ServiceError

logger = logging.getLogger(__name__)

# Monitoring counter: every failed access_grants query locks the actor out
# (fail-closed empty grants). A mass lockout must be visible in /healthz, not
# only in logs.
_grants_query_errors = 0


def grants_query_error_count() -> int:
    return _grants_query_errors


def _record_grants_query_error(exc: Exception) -> None:
    global _grants_query_errors
    _grants_query_errors += 1
    logger.error(
        "access_grants query failed (fail-closed: actor resolves zero grants): %s",
        exc,
        exc_info=True,
    )


_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}
_VALID_ROLES = {"admin", "member", "viewer", "membro"}
_ROLE_RANK = {"viewer": 0, "member": 1, "membro": 1, "admin": 2}
_CLEARANCE_LEVELS = {"public": 0, "internal": 1, "confidential": 2}
_CONFIDENTIAL_RE = re.compile(r"^\s*confidential\s*:\s*(true|yes|1)\s*$", re.I)
_CLEARANCE_RE = re.compile(r"^\s*clearance\s*:\s*(public|internal|confidential)\s*$", re.I)


class AccessGrantError(ServiceError):
    """Domain error for tenant access-grant operations."""


class ProjectWorkspaceOwnershipError(LookupError):
    """A disk-backed project slug has no single provable workspace owner."""


@dataclass(frozen=True)
class AccessGrant:
    identity: str
    project_slug: str
    role: str
    confidential_clearance: bool
    clearance: str
    scope: str
    # Provenance: slug of the team that conferred the winning role, None for a
    # direct grant. Display-only ("via team X") — never part of the predicate.
    via_team: str | None = None


def _env_bool(name: str, *, default: bool | None = None, env: Mapping[str, str] | None = None) -> bool | None:
    source = os.environ if env is None else env
    raw = source.get(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    return default


def _row_get(row: Any, key: str, index: int | None = None, default: Any = None) -> Any:
    if hasattr(row, "keys") and key in row.keys():
        return row[key]
    if index is not None:
        try:
            return row[index]
        except (IndexError, TypeError):
            return default
    return default


def _normalize_role(role: str) -> str:
    value = (role or "").strip().lower()
    if value == "membro":
        value = "member"
    if value not in {"admin", "member", "viewer"}:
        raise AccessGrantError(code="invalid_role", message="role must be admin, member, or viewer")
    return value


def _normalize_clearance(clearance: str | bool | int | None, *, legacy_bool: bool | None = None) -> str:
    if clearance is None:
        return "confidential" if legacy_bool else "internal"
    if isinstance(clearance, bool):
        return "confidential" if clearance else "internal"
    value = str(clearance).strip().lower()
    if value in _TRUE:
        return "confidential"
    if value in _FALSE:
        return "internal"
    if value not in _CLEARANCE_LEVELS:
        raise AccessGrantError(
            code="invalid_clearance",
            message="clearance must be public, internal, or confidential",
        )
    return value


def _normalize_scope(scope: str | None, *, project_slug: str) -> str:
    value = (scope or "project:" + project_slug).strip()
    if value == "all":
        return value
    if value.startswith("project:"):
        target = value.removeprefix("project:").strip()
        if not target:
            raise AccessGrantError(code="invalid_scope", message="project scope requires a slug")
        return f"project:{target}"
    if value.startswith("file:"):
        target = PurePosixPath(value.removeprefix("file:").strip()).as_posix().strip("/")
        if not target:
            raise AccessGrantError(code="invalid_scope", message="file scope requires a path prefix")
        return f"file:{target}"
    raise AccessGrantError(code="invalid_scope", message="scope must be all, project:<slug>, or file:<path-prefix>")


def _clearance_allows(actor_clearance: str, resource_clearance: str) -> bool:
    return _CLEARANCE_LEVELS.get(actor_clearance, -1) >= _CLEARANCE_LEVELS.get(resource_clearance, 0)


def _path_parts(path: str) -> tuple[str | None, str]:
    pure = PurePosixPath((path or "").strip().strip("/"))
    parts = pure.parts
    if not parts:
        return None, ""
    if parts[0] == "projects" and len(parts) >= 2:
        rel = PurePosixPath(*parts[1:]).as_posix()
        return parts[1], rel
    if parts[0] == "repos":
        return None, pure.as_posix()
    return parts[0], pure.as_posix()


def _scope_allows_project(scope: str, project_slug: str) -> bool:
    if scope == "all":
        return True
    if scope == f"project:{project_slug}":
        return True
    if scope.startswith("file:"):
        scoped_project, _ = _path_parts(scope.removeprefix("file:"))
        return scoped_project == project_slug
    return False


def _scope_allows_path(scope: str, project_slug: str, path: str) -> bool:
    if scope == "all" or scope == f"project:{project_slug}":
        return True
    if not scope.startswith("file:"):
        return False
    _, rel = _path_parts(path)
    prefix = PurePosixPath(scope.removeprefix("file:").strip().strip("/")).as_posix()
    return rel == prefix or rel.startswith(prefix.rstrip("/") + "/")


def tenant_multi_user_enabled(env: Mapping[str, str] | None = None) -> bool:
    """Return whether tenant-level grant filtering should be active by default."""
    source = os.environ if env is None else env
    explicit = _env_bool("MARVIS_TENANT_MULTI_USER", default=None, env=source)
    if explicit is not None:
        return explicit
    explicit = _env_bool("MARVIS_MULTI_USER_TENANT", default=None, env=source)
    if explicit is not None:
        return explicit
    tenant_id = (source.get("TENANT_ID") or "").strip()
    configured = {
        item.strip()
        for item in (source.get("MARVIS_MULTI_USER_TENANTS") or "").split(",")
        if item.strip()
    }
    return bool(tenant_id and tenant_id in configured)


def agent_visibility_bypass_enabled(env: Mapping[str, str] | None = None) -> bool:
    """Backward-compatible bypass: explicit env wins; multi-user default is off."""
    explicit = _env_bool("MARVIS_AGENT_VISIBILITY_BYPASS", default=None, env=env)
    if explicit is not None:
        return explicit
    return not tenant_multi_user_enabled(env)


def actor_from_user_info(user: Any) -> CallerContext:
    return CallerContext(
        username=str(getattr(user, "username", "") or ""),
        system_role=str(getattr(user, "system_role", "viewer") or "viewer"),
        user_type=str(getattr(user, "user_type", "human") or "human"),
        workspace_id=str(getattr(user, "workspace_id", "") or ""),
        scopes=tuple(getattr(user, "scopes", []) or []),
        is_human_session=str(getattr(user, "user_type", "human") or "human") == "human",
        user_id=str(getattr(user, "user_id", "") or ""),
        local_runtime=getattr(user, "auth_mechanism", "unknown") == "local",
    )


def unrestricted_actor(actor: CallerContext) -> bool:
    if actor.system_role in {"admin", "super_admin"}:
        return True
    if actor.is_local_os_account:
        return True
    if actor.user_type == "agent" and agent_visibility_bypass_enabled():
        return True
    return False


def _is_local_single_user(actor: CallerContext) -> bool:
    """Local data-plane compatibility, independent of approval authority."""
    return actor.is_local_os_account


def identity_candidates(actor: CallerContext) -> tuple[str, ...]:
    raw = [actor.user_id, actor.username, actor.username.removeprefix("agent:")]
    seen: list[str] = []
    for value in raw:
        value = (value or "").strip()
        if value and value not in seen:
            seen.append(value)
    return tuple(seen)


async def _grant_columns(db: aiosqlite.Connection) -> set[str]:
    cur = await db.execute("PRAGMA table_info(access_grants)")
    return {str(row[1]) for row in await cur.fetchall()}


async def _workspace_isolation_enabled(db: aiosqlite.Connection) -> bool:
    return "workspace_id" in await _grant_columns(db)


async def workspace_isolation_enabled(db: aiosqlite.Connection) -> bool:
    """Whether the canonical workspace-bound grant schema is active."""
    return await _workspace_isolation_enabled(db)


async def resolve_unique_project_workspace(
    db: aiosqlite.Connection,
    *,
    project_slug: str,
    local_workspace_id: str = "ws_default",
    allow_local_single_user: bool = False,
) -> str:
    """Resolve one exact owner for a disk-backed project slug.

    A shared filesystem cannot represent two tenants that use the same slug.
    Remote callers therefore fail closed when the ownership table is absent,
    the slug is unowned, or more than one workspace owns it. The sole
    compatibility exception is an unowned slug in explicit local single-user
    mode for ``ws_default``; ambiguity never bypasses.
    """
    project = (project_slug or "").strip()
    local_workspace = (local_workspace_id or "").strip()
    if not project:
        raise ProjectWorkspaceOwnershipError("Project not found")
    local_compat = False
    if allow_local_single_user and local_workspace == "ws_default":
        from core.api.security import is_local_single_user_mode

        local_compat = is_local_single_user_mode()
    try:
        cursor = await db.execute(
            "SELECT DISTINCT workspace_id FROM workspace_projects "
            "WHERE project_slug = ? AND workspace_id IS NOT NULL "
            "AND length(trim(workspace_id)) > 0 LIMIT 2",
            (project,),
        )
        owners = {str(row[0]) for row in await cursor.fetchall() if row[0]}
    except aiosqlite.Error as exc:
        if local_compat and "no such table: workspace_projects" in str(exc).lower():
            return local_workspace
        raise ProjectWorkspaceOwnershipError("Project not found") from exc
    if len(owners) == 1:
        return next(iter(owners))
    if not owners and local_compat:
        return local_workspace
    raise ProjectWorkspaceOwnershipError("Project not found")


async def require_unique_project_workspace(
    db: aiosqlite.Connection,
    *,
    project_slug: str,
    workspace_id: str,
    allow_local_single_user: bool = False,
) -> str:
    """Require one slug's sole owner to be the expected workspace."""
    workspace = (workspace_id or "").strip()
    if not workspace:
        raise ProjectWorkspaceOwnershipError("Project not found")
    owner = await resolve_unique_project_workspace(
        db,
        project_slug=project_slug,
        local_workspace_id=workspace,
        allow_local_single_user=allow_local_single_user,
    )
    if owner != workspace:
        raise ProjectWorkspaceOwnershipError("Project not found")
    return workspace


async def project_uniquely_owned_by_actor(
    db: aiosqlite.Connection,
    actor: CallerContext,
    project_slug: str | None,
) -> bool:
    """Return whether this actor can safely address the shared project path."""
    project = (project_slug or "").strip()
    if not project:
        return False
    if _is_local_single_user(actor):
        return True
    try:
        await require_unique_project_workspace(
            db,
            project_slug=project,
            workspace_id=require_workspace_ctx(actor),
        )
    except ProjectWorkspaceOwnershipError:
        return False
    return True


async def require_unique_project_for_actor(
    db: aiosqlite.Connection,
    actor: CallerContext,
    project_slug: str,
) -> str:
    """Raise a non-enumerating 404 unless the shared slug is actor-owned."""
    if _is_local_single_user(actor):
        return require_workspace_ctx(actor)
    try:
        return await require_unique_project_workspace(
            db,
            project_slug=project_slug,
            workspace_id=require_workspace_ctx(actor),
        )
    except ProjectWorkspaceOwnershipError as exc:
        raise NotFoundError(code="project_not_found", message="Not found") from exc


async def _workspace_projects(
    db: aiosqlite.Connection, actor: CallerContext
) -> set[str]:
    """Resolve projects from explicit, workspace-owned binding records only."""
    workspace_id = require_workspace_ctx(actor)
    projects: set[str] = set()
    if await _table_exists(db, "workspace_projects"):
        cursor = await db.execute(
            "SELECT project_slug FROM workspace_projects WHERE workspace_id=?",
            (workspace_id,),
        )
        projects.update(str(row[0]) for row in await cursor.fetchall() if row[0])

    # A grant is privileged ownership metadata, not a user-authored artifact.
    # Keep it as a compatibility source for partially upgraded/custom schemas.
    if await _table_exists(db, "access_grants"):
        columns = await _grant_columns(db)
        if "workspace_id" in columns:
            cursor = await db.execute(
                "SELECT DISTINCT project_slug FROM access_grants "
                "WHERE workspace_id=? AND project_slug IS NOT NULL",
                (workspace_id,),
            )
            projects.update(
                str(row[0]) for row in await cursor.fetchall() if row[0]
            )

    if await _table_exists(db, "teams") and await _table_exists(db, "project_teams"):
        team_columns = {
            str(row[1])
            for row in await (
                await db.execute('PRAGMA table_info("teams")')
            ).fetchall()
        }
        if "workspace_id" in team_columns:
            cursor = await db.execute(
                "SELECT DISTINCT pt.project FROM project_teams pt "
                "JOIN teams t ON t.id = pt.team_id "
                "WHERE t.workspace_id = ? AND t.deleted_at IS NULL",
                (workspace_id,),
            )
            projects.update(str(row[0]) for row in await cursor.fetchall() if row[0])

    return projects


async def bind_workspace_project(
    db: aiosqlite.Connection,
    *,
    workspace_id: str,
    project_slug: str,
    source: str,
    created_by: str,
) -> None:
    """Persist canonical workspace ownership during a trusted project action."""
    workspace = (workspace_id or "").strip()
    project = (project_slug or "").strip()
    if not workspace or not project:
        raise AccessGrantError(
            code="workspace_project_binding_invalid",
            message="workspace and project are required",
        )
    if not await _table_exists(db, "workspace_projects"):
        return
    await db.execute(
        "INSERT OR IGNORE INTO workspace_projects "
        "(workspace_id,project_slug,source,created_by) VALUES (?,?,?,?)",
        (workspace, project, source, created_by or None),
    )


async def require_workspace_project_bound(
    db: aiosqlite.Connection,
    actor: CallerContext,
    project_slug: str,
) -> None:
    """Reject mutable artifacts that claim a project outside the workspace."""
    if _is_local_single_user(actor) or not await _workspace_isolation_enabled(db):
        return
    project = (project_slug or "").strip()
    if not project or project not in await _workspace_projects(db, actor):
        raise NotFoundError(code="project_not_found", message="Not found")


async def _workspace_public_projects(
    db: aiosqlite.Connection, actor: CallerContext
) -> set[str]:
    """Return explicitly public projects in the actor's exact workspace.

    ``project_teams.is_public`` is an existing read contract. Workspace
    isolation must constrain it, not silently erase it: a public project is
    readable by workspace members, while a public project owned by another
    workspace remains invisible.
    """
    if not await _table_exists(db, "teams") or not await _table_exists(
        db, "project_teams"
    ):
        return set()
    team_columns = {
        str(row[1])
        for row in await (
            await db.execute('PRAGMA table_info("teams")')
        ).fetchall()
    }
    project_team_columns = {
        str(row[1])
        for row in await (
            await db.execute('PRAGMA table_info("project_teams")')
        ).fetchall()
    }
    if "workspace_id" not in team_columns or "is_public" not in project_team_columns:
        return set()
    cursor = await db.execute(
        "SELECT DISTINCT pt.project FROM project_teams pt "
        "JOIN teams t ON t.id = pt.team_id AND t.deleted_at IS NULL "
        "WHERE pt.is_public = 1 AND t.workspace_id = ?",
        (require_workspace_ctx(actor),),
    )
    return {str(row[0]) for row in await cursor.fetchall() if row[0]}


async def _workspace_wide_project_allowed(
    db: aiosqlite.Connection,
    actor: CallerContext,
    project_slug: str | None,
) -> bool:
    if _is_local_single_user(actor):
        return True
    if not await _workspace_isolation_enabled(db):
        return unrestricted_actor(actor)
    if actor.system_role not in {"admin", "super_admin"} or not project_slug:
        return False
    return project_slug in await _workspace_projects(db, actor)


def _merge_grant(current: AccessGrant | None, grant: AccessGrant) -> AccessGrant:
    if current is None:
        return grant
    # Additive max-wins, each dimension independent. Provenance follows the
    # grant that supplied the winning role (ties keep the current one, so a
    # direct grant loaded first is never re-labelled "via team").
    if _ROLE_RANK.get(grant.role, -1) > _ROLE_RANK.get(current.role, -1):
        role = grant.role
        via_team = grant.via_team
    else:
        role = current.role
        via_team = current.via_team
    clearance = grant.clearance if _CLEARANCE_LEVELS[grant.clearance] > _CLEARANCE_LEVELS[current.clearance] else current.clearance
    if current.scope == "all" or grant.scope == "all":
        scope = "all"
    elif current.scope.startswith("project:"):
        scope = current.scope
    elif grant.scope.startswith("project:"):
        scope = grant.scope
    else:
        scope = grant.scope
    return AccessGrant(
        identity=current.identity or grant.identity,
        project_slug=grant.project_slug,
        role=role,
        confidential_clearance=current.confidential_clearance or grant.confidential_clearance,
        clearance=clearance,
        scope=scope,
        via_team=via_team,
    )


async def _load_direct_grants(db: aiosqlite.Connection, actor: CallerContext) -> dict[str, AccessGrant]:
    identities = identity_candidates(actor)
    if not identities:
        return {}
    placeholders = ",".join("?" for _ in identities)
    try:
        columns = await _grant_columns(db)
        clearance_expr = "clearance" if "clearance" in columns else "NULL AS clearance"
        scope_expr = "scope" if "scope" in columns else "NULL AS scope"
        workspace_clause = ""
        params: list[str] = list(identities)
        if "workspace_id" in columns:
            workspace_clause = " AND workspace_id = ?"
            params.append(require_workspace_ctx(actor))
        cur = await db.execute(
            f"""
            SELECT identity, project_slug, role, confidential_clearance, {clearance_expr}, {scope_expr}
            FROM access_grants
            WHERE identity IN ({placeholders})
              {workspace_clause}
            """,
            params,
        )
        rows = await cur.fetchall()
    except Exception as exc:
        _record_grants_query_error(exc)
        return {}
    grants: dict[str, AccessGrant] = {}
    for row in rows:
        identity = str(_row_get(row, "identity", 0, ""))
        project = str(_row_get(row, "project_slug", 1, ""))
        raw_role = str(_row_get(row, "role", 2, "viewer"))
        role = "member" if raw_role == "membro" else raw_role
        if role not in {"admin", "member", "viewer"}:
            role = "viewer"
        legacy_clearance = bool(_row_get(row, "confidential_clearance", 3, 0))
        clearance = _normalize_clearance(_row_get(row, "clearance", 4, None), legacy_bool=legacy_clearance)
        if legacy_clearance:
            clearance = "confidential"
        scope = _normalize_scope(_row_get(row, "scope", 5, None), project_slug=project)
        grant = AccessGrant(
            identity=identity,
            project_slug=project,
            role=role,
            confidential_clearance=legacy_clearance or clearance == "confidential",
            clearance=clearance,
            scope=scope,
        )
        grants[project] = _merge_grant(grants.get(project), grant)
    return grants


async def _load_team_grants(db: aiosqlite.Connection, actor: CallerContext) -> list[AccessGrant]:
    """Grants conferred by team membership (mig 160).

    Binds ONLY ``actor.user_id`` (the canonical person id — team membership is
    never keyed on emails or agent slugs). ``team_members.is_admin``/``role``
    are lead flags for team MANAGEMENT and never enter this predicate; the
    legacy ``project_teams.is_public`` column is not consumed either. Teams can
    confer at most member+internal — never admin, never confidential.
    Isolated from the direct-grant query: a failure here (e.g. missing 160
    columns) must not lock the actor out of their direct grants.
    """
    user_id = (actor.user_id or "").strip()
    if not user_id or user_id == "local":
        return []
    try:
        cur = await db.execute("PRAGMA table_info(project_teams)")
        pt_columns = {str(row[1]) for row in await cur.fetchall()}
        if not pt_columns:
            return []
        role_expr = "pt.role" if "role" in pt_columns else "NULL"
        clearance_expr = "pt.clearance" if "clearance" in pt_columns else "NULL"
        team_columns = {
            str(row[1])
            for row in await (
                await db.execute('PRAGMA table_info("teams")')
            ).fetchall()
        }
        workspace_clause = ""
        params: list[str] = [user_id]
        if "workspace_id" in team_columns:
            workspace_clause = " AND t.workspace_id = ?"
            params.append(require_workspace_ctx(actor))
        cur = await db.execute(
            f"""
            SELECT pt.project, {role_expr} AS role, {clearance_expr} AS clearance, t.slug AS team_slug
            FROM team_members tm
            JOIN teams t          ON t.id = tm.team_id AND t.deleted_at IS NULL
            JOIN project_teams pt ON pt.team_id = tm.team_id
            WHERE tm.user_id = ?
              {workspace_clause}
            """,
            params,
        )
        rows = await cur.fetchall()
    except Exception as exc:
        _record_grants_query_error(exc)
        return []
    grants: list[AccessGrant] = []
    for row in rows:
        project = str(_row_get(row, "project", 0, "")).strip()
        if not project:
            continue
        role = str(_row_get(row, "role", 1, None) or "member").strip().lower()
        if role not in {"member", "viewer"}:
            role = "member"
        clearance = str(_row_get(row, "clearance", 2, None) or "internal").strip().lower()
        if clearance not in {"public", "internal"}:
            clearance = "internal"
        grants.append(
            AccessGrant(
                identity=user_id,
                project_slug=project,
                role=role,
                confidential_clearance=False,
                clearance=clearance,
                scope=f"project:{project}",
                via_team=str(_row_get(row, "team_slug", 3, "") or "") or None,
            )
        )
    return grants


async def load_grants(db: aiosqlite.Connection, actor: CallerContext) -> dict[str, AccessGrant]:
    """Effective grants: direct rows merged with team-conferred rows.

    Two isolated queries, additive max-wins merge per dimension (never a deny).
    """
    grants = await _load_direct_grants(db, actor)
    for team_grant in await _load_team_grants(db, actor):
        grants[team_grant.project_slug] = _merge_grant(grants.get(team_grant.project_slug), team_grant)
    return grants


async def visible_projects_for_actor(db: aiosqlite.Connection, actor: CallerContext) -> set[str] | None:
    require_workspace_ctx(actor)
    if _is_local_single_user(actor):
        return None
    if not await _workspace_isolation_enabled(db) and unrestricted_actor(actor):
        return None
    if actor.system_role in {"admin", "super_admin"}:
        visible = await _workspace_projects(db, actor)
    else:
        grants = await load_grants(db, actor)
        visible = {
            project
            for project, grant in grants.items()
            if _scope_allows_project(grant.scope, project)
        }
        visible.update(await _workspace_public_projects(db, actor))
    return visible


async def has_confidential_clearance(db: aiosqlite.Connection, actor: CallerContext, project_slug: str | None) -> bool:
    if await _workspace_wide_project_allowed(db, actor, project_slug):
        return True
    if not project_slug:
        return False
    grants = await load_grants(db, actor)
    grant = grants.get(project_slug)
    return bool(grant and (grant.role == "admin" or _clearance_allows(grant.clearance, "confidential")))


async def can_read_project(
    db: aiosqlite.Connection,
    actor: CallerContext,
    project_slug: str | None,
    *,
    confidential: bool = False,
    clearance: str | None = None,
    path: str | None = None,
) -> bool:
    if await _workspace_wide_project_allowed(db, actor, project_slug):
        return True
    if not project_slug:
        return False
    if await _workspace_isolation_enabled(db):
        public_projects = await _workspace_public_projects(db, actor)
        resource_clearance = clearance or (
            "confidential" if confidential else "public"
        )
        if project_slug in public_projects and resource_clearance == "public":
            return True
    grants = await load_grants(db, actor)
    grant = grants.get(project_slug)
    if grant is None:
        return False
    if path is not None and not _scope_allows_path(grant.scope, project_slug, path):
        return False
    if path is None and not _scope_allows_project(grant.scope, project_slug):
        return False
    resource_clearance = clearance or ("confidential" if confidential else "public")
    if grant.role == "admin":
        return True
    return _clearance_allows(grant.clearance, resource_clearance)


async def can_write_project(db: aiosqlite.Connection, actor: CallerContext, project_slug: str | None, *, path: str | None = None) -> bool:
    if await _workspace_wide_project_allowed(db, actor, project_slug):
        return True
    if not project_slug:
        return False
    grants = await load_grants(db, actor)
    grant = grants.get(project_slug)
    if grant is None or grant.role == "viewer":
        return False
    if path is not None:
        return _scope_allows_path(grant.scope, project_slug, path)
    return _scope_allows_project(grant.scope, project_slug) and not grant.scope.startswith("file:")


async def file_writable(db: aiosqlite.Connection, actor: CallerContext, path: str) -> bool:
    """Write-gate (RBAC F4.b): a path with ``file_meta.confidential=1`` is
    writable only by owner/ACL/admin — today a non-reader could overwrite an
    owner-confidential file. Keyed on file_meta (the authoritative record)."""
    from core.api.services import confidential_files

    project = project_from_logical_path(path)
    workspace_id = require_workspace_ctx(actor)
    if not await project_uniquely_owned_by_actor(db, actor, project):
        return False
    if not await can_write_project(db, actor, project, path=path):
        return False
    if await _workspace_wide_project_allowed(db, actor, project):
        return True
    meta = await confidential_files.get_file_meta(
        db, path, workspace_id=workspace_id
    )
    if meta and meta["confidential"]:
        return await confidential_files.actor_cleared_for_file(db, actor, meta)
    return True


def project_from_logical_path(path: str) -> str | None:
    project, _ = _path_parts(path)
    return project


def project_qualified_path(project: str | None, path: str) -> str | None:
    """Bind a repository-relative graph/search path to its owning project.

    Source graph nodes store paths such as ``core/api/db.py`` while the file
    ACL contract expects ``<project>/<relative-path>`` (or
    ``projects/<project>/<relative-path>``).  Treating the first source
    directory as a project made a valid ``marvisx`` grant look like a grant to
    ``core``.  Explicit ``projects/<other>/...`` metadata is never rewritten:
    a disagreement with ``project_id`` fails closed.
    """
    value = (path or "").strip()
    owning_project = (project or "").strip()
    if not value or not owning_project or value.startswith("/"):
        return None
    pure = PurePosixPath(value.strip("/"))
    parts = pure.parts
    if not parts or ".." in parts:
        return None
    if parts[0] == "projects":
        if len(parts) < 2 or parts[1] != owning_project:
            return None
        return pure.as_posix()
    if parts[0] == owning_project:
        return pure.as_posix()
    return PurePosixPath(owning_project, *parts).as_posix()


def _resolve_projects_path(path: str, *, projects_root: Path | None = None) -> Path | None:
    project = project_from_logical_path(path)
    if not project:
        return None
    pure = PurePosixPath((path or "").strip())
    rel = PurePosixPath(*pure.parts[1:]) if pure.parts and pure.parts[0] == "projects" else pure
    root = projects_root
    if root is None:
        raw_root = os.environ.get("MARVIS_PROJECTS_ROOT", "").strip()
        if raw_root:
            root = Path(raw_root).expanduser()
        else:
            try:
                from core.scripts._projects_root import resolve_projects_root
                root = resolve_projects_root()
            except Exception:
                root = Path("/data/projects")
    try:
        base = root.resolve()
        target = (base / rel).resolve()
        target.relative_to(base)
        return target
    except (OSError, ValueError):
        return None


def path_clearance_level(path: str, *, projects_root: Path | None = None) -> str:
    target = _resolve_projects_path(path, projects_root=projects_root)
    if target is None or not target.is_file():
        return "public"
    try:
        head = target.read_text(encoding="utf-8", errors="replace")[:8192]
    except OSError:
        return "public"
    lines = head.splitlines()
    if not lines or lines[0].strip() != "---":
        return "public"
    for line in lines[1:80]:
        stripped = line.strip()
        if stripped == "---":
            return "public"
        if _CONFIDENTIAL_RE.match(line):
            return "confidential"
        match = _CLEARANCE_RE.match(line)
        if match:
            return match.group(1).lower()
    return "public"


def path_has_confidential_frontmatter(path: str, *, projects_root: Path | None = None) -> bool:
    return path_clearance_level(path, projects_root=projects_root) == "confidential"


def metadata_clearance_level(metadata: Any) -> str:
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata) if metadata else {}
        except (TypeError, ValueError):
            metadata = {}
    if not isinstance(metadata, dict):
        return "public"
    clearance = metadata.get("clearance")
    if isinstance(clearance, str) and clearance.strip().lower() in _CLEARANCE_LEVELS:
        return clearance.strip().lower()
    value = metadata.get("confidential")
    if isinstance(value, bool):
        return "confidential" if value else "public"
    if isinstance(value, str) and value.strip().lower() in _TRUE:
        return "confidential"
    return "public"


def metadata_confidential(metadata: Any) -> bool:
    return metadata_clearance_level(metadata) == "confidential"


async def _log_break_glass_read(actor: CallerContext, path: str) -> None:
    """Require a durable audit receipt before an exceptional confidential read."""
    from core.api.db import acquire_write_db
    from core.api.services.audit import log_audit

    async with acquire_write_db(label="confidential.break_glass") as wdb:
        if not wdb.in_transaction:
            await wdb.execute("BEGIN IMMEDIATE")
        await log_audit(
            wdb,
            action="confidential.break_glass_read",
            user=actor.user_id or actor.username,
            resource_type="file",
            resource_id=path,
            details={"reason": "unrestricted_actor_direct_read"},
            workspace_id=require_workspace_ctx(actor),
        )
        await wdb.commit()


async def file_readable(
    db: aiosqlite.Connection,
    actor: CallerContext,
    path: str,
    *,
    direct_read: bool = False,
) -> bool:
    """THE file-read predicate (RBAC F4). Effective confidentiality is
    ``file_meta.confidential OR frontmatter`` — the DB is authoritative, the
    frontmatter can only ADD secrecy (stripping it never declassifies).

    Owner-confidential files are readable ONLY by owner + explicit ACL +
    admin (break-glass, logged on direct reads). Project clearance does NOT
    open them (D5). Owner/ACL still need plain project visibility.
    """
    from core.api.services import confidential_files

    workspace_id = require_workspace_ctx(actor)
    project = project_from_logical_path(path)
    if not await project_uniquely_owned_by_actor(db, actor, project):
        return False
    meta = await confidential_files.get_file_meta(
        db, path, workspace_id=workspace_id
    )
    frontmatter_level = path_clearance_level(path)
    owner_confidential = bool(meta and meta["confidential"]) or frontmatter_level == "confidential"
    if not owner_confidential:
        return await can_read_project(db, actor, project, clearance=frontmatter_level, path=path)
    if await _workspace_wide_project_allowed(db, actor, project):
        if direct_read:
            await _log_break_glass_read(actor, path)
        return True
    if meta and await confidential_files.actor_cleared_for_file(db, actor, meta):
        # cleared identities still need ordinary visibility on the project
        return await can_read_project(db, actor, project, clearance="public", path=path)
    return False


async def can_read_path(db: aiosqlite.Connection, actor: CallerContext, path: str) -> bool:
    return await file_readable(db, actor, path, direct_read=False)


async def filter_search_grouped(actor: CallerContext, grouped: dict[str, list[dict]]) -> dict[str, list[dict]]:
    require_workspace_ctx(actor)
    if _is_local_single_user(actor):
        return grouped
    db = await aiosqlite.connect(settings.db_path)
    db.row_factory = aiosqlite.Row
    try:
        filtered: dict[str, list[dict]] = {}
        for doc_type, items in grouped.items():
            kept: list[dict] = []
            for item in items:
                project = item.get("project")
                path = item.get("path")
                item_path = str(path) if path else None
                if item_path:
                    logical_path = project_qualified_path(
                        str(project) if project else None,
                        item_path,
                    )
                    if logical_path and await file_readable(db, actor, logical_path):
                        kept.append(
                            await _sanitize_search_graph_path(db, actor, item)
                        )
                elif await can_read_project(db, actor, str(project) if project else None):
                    kept.append(
                        await _sanitize_search_graph_path(db, actor, item)
                    )
            filtered[doc_type] = kept
        return filtered
    finally:
        await db.close()


async def _sanitize_search_graph_path(
    db: aiosqlite.Connection,
    actor: CallerContext,
    item: dict,
) -> dict:
    """Remove hidden nodes from search evidence paths before serialization."""
    raw_path = item.get("edge_path")
    if not isinstance(raw_path, list):
        return item
    visible_path: list[str] = []
    for node_id in raw_path:
        node = str(node_id)
        if not await _node_readable(db, actor, node):
            break
        visible_path.append(node)
    cleaned = dict(item)
    cleaned["edge_path"] = visible_path
    if "edge_path_summary" in cleaned:
        cleaned["edge_path_summary"] = " -> ".join(
            node.split(":", 1)[0] if ":" in node else "node"
            for node in visible_path
        )
    return cleaned


async def _node_readable(db: aiosqlite.Connection, actor: CallerContext, node_id: str | None, fallback_project: str | None = None) -> bool:
    if _is_local_single_user(actor):
        return True
    # Preserve the legacy agent-visibility bypass only for databases that have
    # not activated workspace isolation yet.  Once migration 179 adds the
    # workspace-bound grant schema, graph reads remain fail-closed and must use
    # exact project/path evidence.
    if not await _workspace_isolation_enabled(db) and unrestricted_actor(actor):
        return True
    if not node_id:
        return await can_read_project(db, actor, fallback_project)
    cur = await db.execute(
        """
        SELECT project_id, metadata, file_path
        FROM graph_nodes
        WHERE id = ? AND deprecated_at IS NULL
        LIMIT 1
        """,
        (node_id,),
    )
    row = await cur.fetchone()
    if row is None:
        return await can_read_project(db, actor, fallback_project)
    project = row["project_id"] or fallback_project
    path = row["file_path"] if "file_path" in row.keys() else None
    if path:
        # File-backed nodes go through the owner-confidential predicate so
        # project clearance never opens a marked file on graph surfaces (D5).
        logical_path = project_qualified_path(
            str(project) if project else None,
            str(path),
        )
        return bool(
            logical_path
            and await file_readable(db, actor, logical_path)
        )
    clearance = metadata_clearance_level(row["metadata"])
    return await can_read_project(db, actor, project, clearance=clearance)


async def filter_visible_edges_for_actor(db: aiosqlite.Connection, actor: CallerContext, edges: list[dict]) -> tuple[list[dict], int]:
    if _is_local_single_user(actor):
        return edges, 0
    kept: list[dict] = []
    for edge in edges:
        src_ok = await _node_readable(db, actor, edge.get("source_id") or edge.get("source"), edge.get("source_project"))
        tgt_ok = await _node_readable(db, actor, edge.get("target_id") or edge.get("target"), edge.get("target_project"))
        if src_ok and tgt_ok:
            kept.append(edge)
    return kept, len(edges) - len(kept)


async def filter_graph_items(db: aiosqlite.Connection, actor: CallerContext, items: Sequence[dict]) -> list[dict]:
    if _is_local_single_user(actor):
        return list(items)
    kept: list[dict] = []
    for item in items:
        node_id = item.get("node_id") or item.get("id") or item.get("source_id") or item.get("target_id") or item.get("source") or item.get("target")
        project = item.get("project") or item.get("project_id")
        if await _node_readable(db, actor, str(node_id) if node_id else None, str(project) if project else None):
            kept.append(item)
    return kept


async def filter_graph_response(db: aiosqlite.Connection, actor: CallerContext, response: dict) -> dict:
    if _is_local_single_user(actor):
        return response
    require_workspace_ctx(actor)
    out = dict(response)

    root_ids: list[str] = []
    for key in ("node_id", "target"):
        value = out.get(key)
        if isinstance(value, str) and value:
            root_ids.append(value)
    node_value = out.get("node")
    if isinstance(node_value, dict):
        node_id = node_value.get("id") or node_value.get("node_id")
        if isinstance(node_id, str) and node_id:
            root_ids.append(node_id)
    for node_id in root_ids:
        if not await _node_readable(db, actor, node_id):
            raise NotFoundError(code="node_not_found", message="Not found")

    list_keys = (
        "neighbors",
        "direct",
        "direct_dependents",
        "direct_callers",
        "transitive_list",
        "transitive_callers",
        "nodes",
        "hotspots",
        "commits",
        "prs",
        "tasks",
        "handoffs",
        "learnings",
    )
    for key in list_keys:
        value = out.get(key)
        if isinstance(value, list) and all(isinstance(item, dict) for item in value):
            out[key] = await filter_graph_items(db, actor, value)

    resolved_nodes = out.get("resolved_nodes")
    if isinstance(resolved_nodes, list):
        kept_resolved: list[str] = []
        for node_id in resolved_nodes:
            if isinstance(node_id, str) and await _node_readable(db, actor, node_id):
                kept_resolved.append(node_id)
        out["resolved_nodes"] = kept_resolved

    if isinstance(out.get("neighbors"), list):
        neighbors = out["neighbors"]
        out["count"] = len(neighbors)
        relations: dict[str, int] = {}
        directions: dict[str, int] = {}
        distinct_nodes: set[str] = set()
        for item in neighbors:
            edge = item.get("edge") if isinstance(item.get("edge"), dict) else {}
            relation = item.get("relation") or edge.get("relation")
            direction = item.get("direction") or edge.get("direction")
            node_id = item.get("node_id") or item.get("id")
            if relation:
                relations[str(relation)] = relations.get(str(relation), 0) + 1
            if direction:
                directions[str(direction)] = directions.get(str(direction), 0) + 1
            if node_id:
                distinct_nodes.add(str(node_id))
        out["summary"] = {
            "total": len(neighbors),
            "distinct_nodes": len(distinct_nodes),
            "by_relation": relations,
            "by_direction": directions,
            "note": "pre-aggregated from workspace-visible neighbors",
        }
    elif isinstance(out.get("hotspots"), list):
        out["count"] = len(out["hotspots"])

    if isinstance(out.get("direct_callers"), list) and isinstance(
        out.get("transitive_callers"), list
    ):
        direct_callers = out["direct_callers"]
        transitive_callers = out["transitive_callers"]
        summary = {"suspect": 0, "uncertain": 0, "legitimate": 0}
        for item in direct_callers:
            classification = str(item.get("classification") or "")
            if classification in summary:
                summary[classification] += 1
        summary.update(
            direct=len(direct_callers),
            transitive=len(transitive_callers),
            truncated=False,
        )
        out["summary"] = summary

    context_keys = ("commits", "prs", "tasks", "handoffs", "learnings")
    if any(key in out for key in context_keys):
        out["counts"] = {
            key: len(out.get(key, []))
            for key in context_keys
            if isinstance(out.get(key), list)
        }
    if isinstance(out.get("learnings"), list) and "total" in out:
        out["total"] = len(out["learnings"])

    # Claims are counts over the unfiltered graph. Until the graph service can
    # aggregate them inside the workspace predicate, omitting them is safer than
    # leaking hidden-project cardinality.
    out.pop("claims", None)
    return out


async def _actor_is_grant_admin(
    db: aiosqlite.Connection,
    actor: CallerContext,
    project_slug: str | None = None,
) -> bool:
    """Grant-admin predicate: org-admin (unrestricted) OR project-admin.

    With ``project_slug`` the admin grant must be on THAT project: a
    project-admin of X must never mint/revoke grants on Y. ``None`` keeps the
    "admin somewhere" semantics as an entry gate for listing (rows are then
    filtered to the actor's admin projects).
    """
    if project_slug is None:
        if _is_local_single_user(actor):
            return True
        if not await _workspace_isolation_enabled(db) and unrestricted_actor(actor):
            return True
        if actor.system_role in {"admin", "super_admin"}:
            require_workspace_ctx(actor)
            return True
    elif await _workspace_wide_project_allowed(db, actor, project_slug):
        return True
    grants = await load_grants(db, actor)
    if project_slug is None:
        return any(
            grant.role == "admin" and _scope_allows_project(grant.scope, project)
            for project, grant in grants.items()
        )
    grant = grants.get(project_slug)
    return bool(
        grant
        and grant.role == "admin"
        and _scope_allows_project(grant.scope, project_slug)
    )


async def _require_grant_admin(
    db: aiosqlite.Connection,
    actor: CallerContext,
    project_slug: str | None = None,
) -> None:
    if not await _actor_is_grant_admin(db, actor, project_slug):
        raise AccessGrantError(code="scope_denied", message="admin access grant required")


async def is_project_grant_admin(
    db: aiosqlite.Connection,
    actor: CallerContext,
    project_slug: str,
) -> bool:
    """Public predicate for other surfaces: org-admin OR project-admin of THAT project."""
    return await _actor_is_grant_admin(db, actor, project_slug)


async def _table_exists(db: aiosqlite.Connection, name: str) -> bool:
    cur = await db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,))
    return await cur.fetchone() is not None


async def _canonical_identity(
    db: aiosqlite.Connection, identity: str, workspace_id: str
) -> str:
    value = (identity or "").strip()
    if not value:
        raise AccessGrantError(code="identity_required", message="identity is required")
    if not await _table_exists(db, "users"):
        return value
    user_columns = {
        str(row[1])
        for row in await (
            await db.execute('PRAGMA table_info("users")')
        ).fetchall()
    }
    workspace_clause = (
        " AND workspace_id = ?"
        if "workspace_id" in user_columns
        else ""
    )
    params = [value, value, value]
    if workspace_clause:
        params.append(workspace_id)
    cur = await db.execute(
        "SELECT id FROM users WHERE deleted_at IS NULL "
        "AND (id = ? OR slug = ? OR email = ?)"
        f"{workspace_clause} LIMIT 1",
        params,
    )
    row = await cur.fetchone()
    if row is None:
        raise AccessGrantError(code="identity_not_found", message="identity is not a tenant member")
    return str(row["id"] if hasattr(row, "keys") else row[0])


def _project_root(project_slug: str) -> Path:
    raw_root = os.environ.get("MARVIS_PROJECTS_ROOT", "").strip()
    root = Path(raw_root).expanduser() if raw_root else Path("/data/projects")
    return (root / project_slug).resolve()


async def _validate_project(project_slug: str) -> str:
    project = (project_slug or "").strip()
    if not re.fullmatch(r"[a-z0-9][a-z0-9_&-]{0,127}", project):
        raise AccessGrantError(code="invalid_project_slug", message="project slug is invalid")
    raw_root = os.environ.get("MARVIS_PROJECTS_ROOT", "").strip()
    if raw_root:
        base = Path(raw_root).expanduser().resolve()
        target = (base / project).resolve()
        try:
            target.relative_to(base)
        except ValueError as exc:
            raise AccessGrantError(code="invalid_project_slug", message="project escapes projects root") from exc
        if not target.is_dir():
            raise AccessGrantError(code="project_not_found", message="project not found in tenant")
    return project


async def _audit_access_grant(db: aiosqlite.Connection, *, action: str, actor: CallerContext, identity: str, project_slug: str, role: str | None = None, clearance: str | None = None, scope: str | None = None) -> None:
    from core.api.services.audit import log_audit

    details = {
        "project_slug": project_slug,
        "role": role,
        "clearance": clearance,
        "scope": scope,
    }
    await log_audit(
        db,
        action=f"access_grants.{action}",
        user=actor.user_id or actor.username,
        resource_type="access_grant",
        resource_id=f"{identity}:{project_slug}",
        details=details,
        workspace_id=require_workspace_ctx(actor),
    )


async def grant_access(db: aiosqlite.Connection, actor: CallerContext, *, identity: str, project_slug: str, role: str, clearance: str, scope: str | None = None) -> AccessGrant:
    # Permission check FIRST (with the raw slug) so non-admins cannot probe
    # project existence through validation error codes.
    await _require_grant_admin(db, actor, (project_slug or "").strip())
    project = await _validate_project(project_slug)
    workspace_id = require_workspace_ctx(actor)
    canonical_identity = await _canonical_identity(db, identity, workspace_id)
    normalized_role = _normalize_role(role)
    normalized_clearance = _normalize_clearance(clearance)
    normalized_scope = _normalize_scope(scope, project_slug=project)
    confidential = 1 if normalized_clearance == "confidential" else 0
    columns = await _grant_columns(db)
    if "workspace_id" in columns:
        cursor = await db.execute(
            """
            INSERT INTO access_grants(
                identity, project_slug, role, confidential_clearance, clearance,
                scope, updated_at, workspace_id
            ) VALUES (?, ?, ?, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%fZ','now'), ?)
            ON CONFLICT DO UPDATE SET
                role = excluded.role,
                confidential_clearance = excluded.confidential_clearance,
                clearance = excluded.clearance,
                scope = excluded.scope,
                updated_at = excluded.updated_at
            WHERE access_grants.workspace_id = excluded.workspace_id
            """,
            (
                canonical_identity,
                project,
                normalized_role,
                confidential,
                normalized_clearance,
                normalized_scope,
                workspace_id,
            ),
        )
        if cursor.rowcount != 1:
            raise AccessGrantError(
                code="cross_workspace_grant_conflict",
                message="Access grant belongs to a different workspace",
            )
    else:
        await db.execute(
            """
            INSERT INTO access_grants(identity, project_slug, role,
                confidential_clearance, clearance, scope, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%fZ','now'))
            ON CONFLICT(identity, project_slug) DO UPDATE SET
                role = excluded.role,
                confidential_clearance = excluded.confidential_clearance,
                clearance = excluded.clearance,
                scope = excluded.scope,
                updated_at = excluded.updated_at
            """,
            (
                canonical_identity,
                project,
                normalized_role,
                confidential,
                normalized_clearance,
                normalized_scope,
            ),
        )
    await _audit_access_grant(db, action="grant", actor=actor, identity=canonical_identity, project_slug=project, role=normalized_role, clearance=normalized_clearance, scope=normalized_scope)
    await db.commit()
    return AccessGrant(canonical_identity, project, normalized_role, bool(confidential), normalized_clearance, normalized_scope)


async def seed_creator_grant(
    db: aiosqlite.Connection,
    *,
    identity: str,
    project_slug: str,
    granted_by: str = "",
    workspace_id: str = "ws_default",
    commit: bool = True,
) -> None:
    """Bootstrap grant for a project creator (RBAC F2.6).

    admin + confidential clearance, scope ``project:<slug>``. Deliberately NOT
    gated on grant-admin: at creation time nobody holds a grant on the new
    project yet. Callers decide WHO qualifies (persons, or an explicit
    ``owner=`` named by a service caller). INSERT OR IGNORE: never clobbers an
    existing row.
    """
    canonical_identity = await _canonical_identity(db, identity, workspace_id)
    project = (project_slug or "").strip()
    if not project:
        raise AccessGrantError(code="invalid_project_slug", message="project slug is required")
    columns = await _grant_columns(db)
    if "workspace_id" in columns:
        cursor = await db.execute(
            """
            INSERT OR IGNORE INTO access_grants(
                identity, project_slug, role, confidential_clearance, clearance,
                scope, updated_at, workspace_id
            ) VALUES (?, ?, 'admin', 1, 'confidential', ?,
                strftime('%Y-%m-%dT%H:%M:%fZ','now'), ?)
            """,
            (canonical_identity, project, f"project:{project}", workspace_id),
        )
    else:
        cursor = await db.execute(
            """
            INSERT OR IGNORE INTO access_grants(
                identity, project_slug, role, confidential_clearance, clearance,
                scope, updated_at
            ) VALUES (?, ?, 'admin', 1, 'confidential', ?,
                strftime('%Y-%m-%dT%H:%M:%fZ','now'))
            """,
            (canonical_identity, project, f"project:{project}"),
        )
    if cursor.rowcount > 0:
        await _audit_access_grant(
            db, action="creator_grant",
            actor=CallerContext(
                username=granted_by or canonical_identity,
                system_role="operator",
                user_type="human",
                workspace_id=workspace_id,
                scopes=(),
                user_id=granted_by or canonical_identity,
            ),
            identity=canonical_identity, project_slug=project,
            role="admin", clearance="confidential", scope=f"project:{project}",
        )
    if commit:
        await db.commit()


async def revoke_access(db: aiosqlite.Connection, actor: CallerContext, *, identity: str, project_slug: str) -> bool:
    await _require_grant_admin(db, actor, (project_slug or "").strip())
    project = await _validate_project(project_slug)
    workspace_id = require_workspace_ctx(actor)
    canonical_identity = await _canonical_identity(db, identity, workspace_id)
    columns = await _grant_columns(db)
    workspace_clause = " AND workspace_id = ?" if "workspace_id" in columns else ""
    params = [canonical_identity, project]
    if workspace_clause:
        params.append(workspace_id)
    cur = await db.execute(
        "DELETE FROM access_grants WHERE identity = ? AND project_slug = ?"
        + workspace_clause,
        params,
    )
    removed = cur.rowcount > 0
    if removed:
        await _audit_access_grant(db, action="revoke", actor=actor, identity=canonical_identity, project_slug=project)
    await db.commit()
    return removed


async def list_access(db: aiosqlite.Connection, actor: CallerContext, *, identity: str | None = None) -> list[AccessGrant]:
    await _require_grant_admin(db, actor)
    workspace_id = require_workspace_ctx(actor)
    columns = await _grant_columns(db)
    admin_projects: set[str] | None = None
    if "workspace_id" in columns and actor.system_role in {"admin", "super_admin"}:
        admin_projects = await _workspace_projects(db, actor)
    elif not unrestricted_actor(actor):
        actor_grants = await load_grants(db, actor)
        admin_projects = {
            project
            for project, grant in actor_grants.items()
            if grant.role == "admin" and _scope_allows_project(grant.scope, project)
        }
    conditions: list[str] = []
    params: list[str] = []
    if "workspace_id" in columns:
        conditions.append("workspace_id = ?")
        params.append(workspace_id)
    if identity:
        canonical_identity = await _canonical_identity(db, identity, workspace_id)
        conditions.append("identity = ?")
        params.append(canonical_identity)
    where = "WHERE " + " AND ".join(conditions) if conditions else ""
    clearance_expr = "clearance" if "clearance" in columns else "NULL AS clearance"
    scope_expr = "scope" if "scope" in columns else "NULL AS scope"
    cur = await db.execute(
        f"SELECT identity, project_slug, role, confidential_clearance, {clearance_expr}, {scope_expr} FROM access_grants {where} ORDER BY identity, project_slug",
        params,
    )
    rows = await cur.fetchall()
    grants: list[AccessGrant] = []
    for row in rows:
        legacy_clearance = bool(_row_get(row, "confidential_clearance", 3, 0))
        project = str(_row_get(row, "project_slug", 1, ""))
        if admin_projects is not None and project not in admin_projects:
            continue
        clearance_value = _normalize_clearance(_row_get(row, "clearance", 4, None), legacy_bool=legacy_clearance)
        if legacy_clearance:
            clearance_value = "confidential"
        role_value = _normalize_role(str(_row_get(row, "role", 2, "viewer")))
        grants.append(
            AccessGrant(
                identity=str(_row_get(row, "identity", 0, "")),
                project_slug=project,
                role=role_value,
                confidential_clearance=legacy_clearance or clearance_value == "confidential",
                clearance=clearance_value,
                scope=_normalize_scope(_row_get(row, "scope", 5, None), project_slug=project),
            )
        )
    return grants
