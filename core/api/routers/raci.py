# v1.3.0 - 2026-03-10 - Remove hardcoded _SYSTEM_ACTOR, use authenticated user for attribution
from __future__ import annotations

import logging
from datetime import datetime, timezone

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException

from core.api.db import get_db, get_write_db
from core.api.models import RaciAddRequest, RaciEntry, RaciReplaceRequest, UserSummary
from core.api.rbac import require_role
from core.api.routers._adapter import to_http
from core.api.security import get_current_user_or_agent
from core.api.services import access_grants, project_lifecycle
from core.api.services.events import emit_event
from core.api.use_cases._context import CallerContext, require_workspace_ctx
from core.api.use_cases._errors import ServiceError

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/projects", tags=["raci"])


async def _get_raci_with_users(
    db: aiosqlite.Connection,
    project: str,
    *,
    workspace_id: str | None,
) -> list[RaciEntry]:
    """Fetch RACI entries, filtering polluted foreign user references."""
    db.row_factory = aiosqlite.Row
    query = (
        "SELECT r.role, u.id, u.slug, u.display_name, u.avatar_color "
        "FROM project_raci r JOIN users u ON u.id = r.user_id "
        "WHERE r.project = ? AND u.deleted_at IS NULL"
    )
    params: list[str] = [project]
    if workspace_id is not None:
        query += " AND u.workspace_id = ?"
        params.append(workspace_id)
    query += (
        " ORDER BY CASE r.role "
        "WHEN 'responsible' THEN 1 WHEN 'accountable' THEN 2 "
        "WHEN 'consulted' THEN 3 WHEN 'informed' THEN 4 END, u.display_name"
    )
    rows = await (await db.execute(query, params)).fetchall()
    return [
        RaciEntry(
            user=UserSummary(
                id=row["id"],
                slug=row["slug"],
                display_name=row["display_name"],
                avatar_color=row["avatar_color"] or "#6366f1",
            ),
            role=row["role"],
        )
        for row in rows
    ]


async def _require_workspace_project(
    db: aiosqlite.Connection, user, project: str
) -> str:
    """Return workspace for an unambiguous visible slug; local stdio bypasses."""
    ctx = CallerContext.from_user_info(
        user, is_human_session=getattr(user, "user_type", "human") == "human"
    )
    visible = await access_grants.visible_projects_for_actor(db, ctx)
    if visible is not None and project not in visible:
        raise HTTPException(status_code=404, detail="Not found")
    if ctx.user_id == "local" and ctx.username == "local":
        return require_workspace_ctx(ctx)
    workspace_id = require_workspace_ctx(ctx)
    try:
        owners = {
            str(row[0])
            for row in await (
                await db.execute(
                    "SELECT workspace_id FROM workspace_projects "
                    "WHERE project_slug = ?",
                    (project,),
                )
            ).fetchall()
            if row[0]
        }
    except aiosqlite.Error:
        owners = set()
    if owners != {workspace_id}:
        raise HTTPException(status_code=404, detail="Not found")
    return workspace_id


async def _record_raci_write(
    db: aiosqlite.Connection,
    user,
    *,
    workspace_id: str,
    project: str,
    resource_ref: str,
) -> None:
    """Journal the RACI mutation so archived projects remain immutable."""
    ctx = CallerContext.from_user_info(
        user, is_human_session=getattr(user, "user_type", "human") == "human"
    )
    try:
        await project_lifecycle.record_project_write(
            db,
            workspace_id=workspace_id,
            project_slug=project,
            writer_kind="project_raci",
            actor=ctx.user_id or ctx.username,
            resource_ref=resource_ref,
        )
    except ServiceError as exc:
        raise to_http(exc) from exc


@router.get("/{slug}/raci", response_model=list[RaciEntry])
async def get_raci(
    slug: str,
    user=Depends(get_current_user_or_agent),
    db: aiosqlite.Connection = Depends(get_db),
):
    """Lista RACI del progetto con dati utente embedded."""
    workspace_id = await _require_workspace_project(db, user, slug)
    return await _get_raci_with_users(db, slug, workspace_id=workspace_id)


@router.post("/{slug}/raci", response_model=list[RaciEntry], status_code=201)
async def add_raci_entry(
    slug: str,
    body: RaciAddRequest,
    user=Depends(require_role("operator", "admin", "super_admin", human_only=True)),
    db: aiosqlite.Connection = Depends(get_write_db),
):
    """Aggiungi singola entry RACI.

    Fallisce con 409 se Responsible o Accountable sono gia presenti nel progetto
    (partial unique index garantisce uno solo per progetto).
    """
    db.row_factory = aiosqlite.Row
    workspace_id = await _require_workspace_project(db, user, slug)

    # Verifica utente esiste e non e eliminato
    if workspace_id is None:
        u = await (
            await db.execute(
                "SELECT id FROM users WHERE id = ? AND deleted_at IS NULL",
                (body.user_id,),
            )
        ).fetchone()
    else:
        u = await (
            await db.execute(
                "SELECT id FROM users WHERE id = ? AND workspace_id = ? "
                "AND deleted_at IS NULL",
                (body.user_id, workspace_id),
            )
        ).fetchone()
    if not u:
        raise HTTPException(status_code=404, detail="User not found")

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    await _record_raci_write(
        db,
        user,
        workspace_id=workspace_id,
        project=slug,
        resource_ref=f"{body.user_id}:{body.role}",
    )
    try:
        await db.execute(
            "INSERT INTO project_raci (project, user_id, role) VALUES (?, ?, ?)",
            (slug, body.user_id, body.role),
        )
        await db.execute(
            "INSERT INTO project_raci_history "
            "(project, user_id, role, action, changed_by, changed_at, reason) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (slug, body.user_id, body.role, "assign", user.user_id or user.username, now, body.reason),
        )
        await db.commit()
    except Exception as exc:
        await db.rollback()
        if "UNIQUE" in str(exc):
            raise HTTPException(
                status_code=409,
                detail=f"Conflict: role '{body.role}' already assigned in project '{slug}'",
            )
        raise

    await emit_event(
        db,
        "raci.updated",
        project=slug,
        target_type="raci",
        target_id=slug,
        payload={"action": "assign", "user_id": body.user_id, "role": body.role},
        workspace_id=workspace_id,
    )
    return await _get_raci_with_users(db, slug, workspace_id=workspace_id)


@router.put("/{slug}/raci", response_model=list[RaciEntry])
async def replace_raci(
    slug: str,
    body: RaciReplaceRequest,
    user=Depends(require_role("operator", "admin", "super_admin", human_only=True)),
    db: aiosqlite.Connection = Depends(get_write_db),
):
    """Replace completo RACI del progetto (idempotente).

    Revoca tutte le entry esistenti e reinserisce quelle fornite.
    Tutto registrato in project_raci_history per audit trail.
    """
    workspace_id = await _require_workspace_project(db, user, slug)
    db.row_factory = aiosqlite.Row
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    if body.entries and workspace_id is not None:
        requested_ids = sorted({entry.user_id for entry in body.entries})
        placeholders = ",".join("?" for _ in requested_ids)
        rows = await (
            await db.execute(
                f"SELECT id FROM users WHERE id IN ({placeholders}) "
                "AND workspace_id = ? AND deleted_at IS NULL",
                [*requested_ids, workspace_id],
            )
        ).fetchall()
        if {str(row[0]) for row in rows} != set(requested_ids):
            raise HTTPException(status_code=404, detail="Not found")

    # Leggi entries esistenti per audit trail
    async with db.execute(
        "SELECT user_id, role FROM project_raci WHERE project = ?", (slug,)
    ) as cursor:
        existing = await cursor.fetchall()

    await _record_raci_write(
        db,
        user,
        workspace_id=workspace_id,
        project=slug,
        resource_ref="replace",
    )

    # Cancella entries correnti
    await db.execute("DELETE FROM project_raci WHERE project = ?", (slug,))

    # Registra revoca per audit
    for row in existing:
        await db.execute(
            "INSERT INTO project_raci_history "
            "(project, user_id, role, action, changed_by, changed_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (slug, row["user_id"], row["role"], "revoke", user.user_id or user.username, now),
        )

    # Reinserisci nuove entries
    for entry in body.entries:
        await db.execute(
            "INSERT OR IGNORE INTO project_raci (project, user_id, role) VALUES (?, ?, ?)",
            (slug, entry.user_id, entry.role),
        )
        await db.execute(
            "INSERT INTO project_raci_history "
            "(project, user_id, role, action, changed_by, changed_at, reason) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                slug,
                entry.user_id,
                entry.role,
                "assign",
                user.user_id or user.username,
                now,
                entry.reason,
            ),
        )

    await db.commit()

    await emit_event(
        db,
        "raci.updated",
        project=slug,
        target_type="raci",
        target_id=slug,
        payload={"action": "replace", "entries": len(body.entries)},
        workspace_id=workspace_id,
    )
    return await _get_raci_with_users(db, slug, workspace_id=workspace_id)


@router.delete("/{slug}/raci/{user_id}/{role}", status_code=204)
async def remove_raci_entry(
    slug: str,
    user_id: str,
    role: str,
    user=Depends(require_role("operator", "admin", "super_admin", human_only=True)),
    db: aiosqlite.Connection = Depends(get_write_db),
):
    """Rimuovi singola entry RACI. Registra revoca in audit trail."""
    workspace_id = await _require_workspace_project(db, user, slug)
    if workspace_id is not None:
        target_user = await (
            await db.execute(
                "SELECT id FROM users WHERE id = ? AND workspace_id = ? "
                "AND deleted_at IS NULL",
                (user_id, workspace_id),
            )
        ).fetchone()
        if target_user is None:
            raise HTTPException(status_code=404, detail="RACI entry not found")
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    await _record_raci_write(
        db,
        user,
        workspace_id=workspace_id,
        project=slug,
        resource_ref=f"{user_id}:{role}",
    )
    result = await db.execute(
        "DELETE FROM project_raci WHERE project = ? AND user_id = ? AND role = ?",
        (slug, user_id, role),
    )
    if result.rowcount == 0:
        await db.rollback()
        raise HTTPException(status_code=404, detail="RACI entry not found")

    await db.execute(
        "INSERT INTO project_raci_history "
        "(project, user_id, role, action, changed_by, changed_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (slug, user_id, role, "revoke", user.user_id or user.username, now),
    )
    await db.commit()

    await emit_event(
        db,
        "raci.updated",
        project=slug,
        target_type="raci",
        target_id=slug,
        payload={"action": "revoke", "user_id": user_id, "role": role},
        workspace_id=workspace_id,
    )
