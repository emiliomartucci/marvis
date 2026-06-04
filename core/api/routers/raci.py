# v1.3.0 - 2026-03-10 - Remove hardcoded _SYSTEM_ACTOR, use authenticated user for attribution
from __future__ import annotations

import logging
from datetime import datetime, timezone

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException

from core.api.db import get_db, get_write_db
from core.api.models import RaciEntry, RaciAddRequest, RaciReplaceRequest, UserInfo, UserSummary
from core.api.rbac import require_role
from core.api.security import get_current_user, get_current_user_or_agent
from core.api.services.events import emit_event
from core.api.visibility import check_project_access

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/projects", tags=["raci"])


async def _get_raci_with_users(
    db: aiosqlite.Connection, project: str
) -> list[RaciEntry]:
    """Fetch RACI entries with embedded user summaries, excluding soft-deleted users."""
    db.row_factory = aiosqlite.Row
    async with db.execute(
        """
        SELECT r.role, u.id, u.slug, u.display_name, u.avatar_color
        FROM project_raci r
        JOIN users u ON u.id = r.user_id
        WHERE r.project = ? AND u.deleted_at IS NULL
        ORDER BY
            CASE r.role
                WHEN 'responsible' THEN 1
                WHEN 'accountable' THEN 2
                WHEN 'consulted'   THEN 3
                WHEN 'informed'    THEN 4
            END,
            u.display_name
        """,
        (project,),
    ) as cursor:
        rows = await cursor.fetchall()

    return [
        RaciEntry(
            user=UserSummary(
                id=r["id"],
                slug=r["slug"],
                display_name=r["display_name"],
                avatar_color=r["avatar_color"] or "#6366f1",
            ),
            role=r["role"],
        )
        for r in rows
    ]


@router.get("/{slug}/raci", response_model=list[RaciEntry])
async def get_raci(
    slug: str,
    user=Depends(get_current_user_or_agent),
    db: aiosqlite.Connection = Depends(get_db),
):
    """Lista RACI del progetto con dati utente embedded."""
    await check_project_access(slug, user, db)
    return await _get_raci_with_users(db, slug)


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

    # Verifica utente esiste e non e eliminato
    u = await (
        await db.execute(
            "SELECT id FROM users WHERE id = ? AND deleted_at IS NULL",
            (body.user_id,),
        )
    ).fetchone()
    if not u:
        raise HTTPException(status_code=404, detail="User not found")

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

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
    )
    return await _get_raci_with_users(db, slug)


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
    await check_project_access(slug, user, db)
    db.row_factory = aiosqlite.Row
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    # Leggi entries esistenti per audit trail
    async with db.execute(
        "SELECT user_id, role FROM project_raci WHERE project = ?", (slug,)
    ) as cursor:
        existing = await cursor.fetchall()

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
    )
    return await _get_raci_with_users(db, slug)


@router.delete("/{slug}/raci/{user_id}/{role}", status_code=204)
async def remove_raci_entry(
    slug: str,
    user_id: str,
    role: str,
    user=Depends(require_role("operator", "admin", "super_admin", human_only=True)),
    db: aiosqlite.Connection = Depends(get_write_db),
):
    """Rimuovi singola entry RACI. Registra revoca in audit trail."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    result = await db.execute(
        "DELETE FROM project_raci WHERE project = ? AND user_id = ? AND role = ?",
        (slug, user_id, role),
    )
    if result.rowcount == 0:
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
    )
