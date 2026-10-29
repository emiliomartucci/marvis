"""Explicit owner reconciliation for legacy single-workspace tenants.

Lifecycle metadata is a consistency check, never ownership authority. Applying
this operation is an authenticated administrator's explicit owner declaration.
Normal project authorization continues to require the canonical binding.
"""
from __future__ import annotations

import re
from pathlib import Path

import aiosqlite

from core.api.services.access_grants import bind_workspace_project
from core.api.services.audit import log_audit
from core.api.services.project_lifecycle import (
    async_project_mutation_guard,
    project_digest,
    project_directory,
)
from core.api.use_cases._context import CallerContext, require_role_ctx, require_workspace_ctx
from core.api.use_cases._errors import AuthorizationError, ConflictError, ServiceError, ValidationError


async def _status(db, slug: str, workspace: str, root: Path) -> str:
    owners = await (await db.execute(
        "SELECT workspace_id FROM workspace_projects WHERE project_slug=?", (slug,),
    )).fetchall()
    if owners:
        return "already_bound" if len(owners) == 1 and owners[0][0] == workspace else "ineligible"
    rows = await (await db.execute(
        "SELECT workspace_id,lifecycle,project_digest,transition_operation_id "
        "FROM project_lifecycle_state WHERE project_slug=?", (slug,),
    )).fetchall()
    if len(rows) != 1:
        return "ineligible"
    row = rows[0]
    if row[0] != workspace or row[1] == "archived" or row[3]:
        return "ineligible"
    try:
        directory = project_directory(slug, projects_root=root)
        if project_digest(directory) != row[2]:
            return "ineligible"
    except (ServiceError, OSError):
        return "ineligible"
    return "eligible"


async def reconcile_project_ownership(
    db: aiosqlite.Connection,
    actor: CallerContext,
    *,
    projects: list[str],
    workspace_id: str,
    apply: bool = False,
    projects_root: Path | None = None,
) -> dict:
    """Preview or atomically bind an explicit list; never enumerate or grant access."""
    require_role_ctx(actor, "admin", "super_admin")
    if (
        not actor.can_act_as_approver
        or not actor.user_id.strip()
        or actor.auth_mechanism not in {"session", "mcp_oauth_person"}
        or workspace_id != require_workspace_ctx(actor)
    ):
        raise AuthorizationError(
            code="ownership_declaration_required",
            message="An authenticated human administrator must declare ownership in their workspace",
        )
    if (
        not 1 <= len(projects) <= 50
        or len(set(projects)) != len(projects)
        or any(not re.fullmatch(r"[a-z0-9][a-z0-9&+_.\-]{0,62}", p) for p in projects)
    ):
        raise ValidationError(code="invalid_project_selection", message="Select 1 to 50 distinct valid project slugs")
    if db.in_transaction:
        raise ConflictError(code="ownership_transaction_active", message="A dedicated transaction is required")
    if projects_root is None:
        from core.api.use_cases.projects import data_project_dir

        projects_root = data_project_dir()
    root = Path(projects_root).expanduser()
    if root.is_symlink() or not root.is_dir():
        raise ConflictError(code="ownership_root_unavailable", message="Project root must be an existing directory")
    async with async_project_mutation_guard(projects_root=root):
        try:
            await db.execute("BEGIN IMMEDIATE" if apply else "BEGIN")
            workspaces = await (await db.execute("SELECT id FROM workspaces")).fetchall()
            if len(workspaces) != 1 or workspaces[0][0] != workspace_id:
                raise ConflictError(
                    code="ownership_recovery_requires_single_workspace",
                    message="Legacy recovery requires a tenant with one matching workspace",
                )
            results = [
                {"project": slug, "status": await _status(db, slug, workspace_id, root)}
                for slug in projects
            ]
            repaired = []
            if apply:
                if any(item["status"] == "ineligible" for item in results):
                    raise ConflictError(
                        code="ownership_selection_ineligible",
                        message="Selection cannot be reconciled; no ownership changes were made",
                    )
                for item in results:
                    if item["status"] == "already_bound":
                        continue
                    slug = item["project"]
                    await bind_workspace_project(
                        db, workspace_id=workspace_id, project_slug=slug,
                        source="migration", created_by=actor.user_id,
                    )
                    await log_audit(
                        db, "project.ownership_recovered", actor.user_id, "project", slug,
                        {"ownership_basis": "explicit_owner_declaration"},
                        workspace_id=workspace_id,
                    )
                    repaired.append(slug)
            await db.commit()
            return {"workspace_id": workspace_id, "applied": apply, "projects": results, "repaired": repaired}
        except BaseException:
            await db.rollback()
            raise
