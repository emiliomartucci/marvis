# v1.1.0 - 2026-03-10 - Add RBAC operator+ check on POST status update
from __future__ import annotations

import logging
from datetime import datetime, timezone

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException, Query

from core.api.db import get_db, get_write_db
from core.api.models import StatusUpdateCreateRequest, StatusUpdateResponse, UserInfo
from core.api.rbac import require_role
from core.api.security import get_current_user_or_agent
from core.api.services import access_grants
from core.api.use_cases._context import CallerContext, require_workspace_ctx

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/status-updates", tags=["status-updates"])


async def _visible_unique_projects(
    db: aiosqlite.Connection, user: UserInfo
) -> set[str] | None:
    """Projects safely representable by the legacy slug-only status table."""
    ctx = CallerContext.from_user_info(
        user, is_human_session=user.user_type == "human"
    )
    visible = await access_grants.visible_projects_for_actor(db, ctx)
    if ctx.user_id == "local" and ctx.username == "local":
        return None
    workspace_id = require_workspace_ctx(ctx)
    try:
        rows = await (
            await db.execute(
                "SELECT project_slug FROM workspace_projects "
                "GROUP BY project_slug HAVING COUNT(DISTINCT workspace_id) = 1 "
                "AND MIN(workspace_id) = ?",
                (workspace_id,),
            )
        ).fetchall()
    except aiosqlite.Error:
        return set()
    unique = {str(row[0]) for row in rows if row[0]}
    return unique if visible is None else unique & visible


async def _require_project(
    db: aiosqlite.Connection, user: UserInfo, project: str
) -> None:
    allowed = await _visible_unique_projects(db, user)
    if allowed is not None and project not in allowed:
        raise HTTPException(status_code=404, detail="Not found")


def _row_to_response(row: aiosqlite.Row) -> StatusUpdateResponse:
    return StatusUpdateResponse(
        id=row["id"],
        project=row["project"],
        status=row["status"],
        what_done=row["what_done"],
        blockers=row["blockers"],
        next_steps=row["next_steps"],
        created_by=row["created_by"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


@router.post("", status_code=201)
async def create_status_update(
    body: StatusUpdateCreateRequest,
    user: UserInfo = Depends(require_role("operator")),
    db: aiosqlite.Connection = Depends(get_write_db),
) -> StatusUpdateResponse:
    """Create a project status update."""
    await _require_project(db, user, body.project)
    now = datetime.now(timezone.utc).isoformat()
    cursor = await db.execute(
        "INSERT INTO project_status_updates (project, status, what_done, blockers, next_steps, created_by, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (body.project, body.status, body.what_done, body.blockers, body.next_steps, user.username, now),
    )
    await db.commit()

    cursor = await db.execute(
        "SELECT * FROM project_status_updates WHERE id = ?", (cursor.lastrowid,)
    )
    row = await cursor.fetchone()
    return _row_to_response(row)


@router.get("")
async def list_status_updates(
    project: str | None = None,
    status: str | None = None,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    user: UserInfo = Depends(get_current_user_or_agent),
    db: aiosqlite.Connection = Depends(get_db),
) -> list[StatusUpdateResponse]:
    """List status updates with filters."""
    allowed = await _visible_unique_projects(db, user)
    conditions: list[str] = []
    params: list = []
    if project:
        if allowed is not None and project not in allowed:
            raise HTTPException(status_code=404, detail="Not found")
        conditions.append("project = ?")
        params.append(project)
    elif allowed is not None:
        if not allowed:
            return []
        placeholders = ",".join("?" for _ in allowed)
        conditions.append(f"project IN ({placeholders})")
        params.extend(sorted(allowed))
    if status:
        conditions.append("status = ?")
        params.append(status)

    where = " AND ".join(conditions) if conditions else "1=1"
    query = f"SELECT * FROM project_status_updates WHERE {where} ORDER BY created_at DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])

    cursor = await db.execute(query, params)
    return [_row_to_response(row) async for row in cursor]


@router.get("/overdue")
async def get_overdue_projects(
    user: UserInfo = Depends(get_current_user_or_agent),
    db: aiosqlite.Connection = Depends(get_db),
) -> list[StatusUpdateResponse]:
    """Projects with active status but no update in >7 days."""
    allowed = await _visible_unique_projects(db, user)
    query = (
        "SELECT * FROM project_status_updates "
        "WHERE id IN (SELECT MAX(id) FROM project_status_updates GROUP BY project) "
        "AND status = 'active' "
        "AND created_at < datetime('now', '-7 days')"
    )
    params: list[str] = []
    if allowed is not None:
        if not allowed:
            return []
        placeholders = ",".join("?" for _ in allowed)
        query += f" AND project IN ({placeholders})"
        params.extend(sorted(allowed))
    query += " ORDER BY created_at ASC"
    cursor = await db.execute(query, params)
    return [_row_to_response(row) async for row in cursor]
