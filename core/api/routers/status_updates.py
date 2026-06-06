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

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/status-updates", tags=["status-updates"])


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
    conditions: list[str] = []
    params: list = []
    if project:
        conditions.append("project = ?")
        params.append(project)
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
    cursor = await db.execute(
        "SELECT * FROM project_status_updates "
        "WHERE id IN (SELECT MAX(id) FROM project_status_updates GROUP BY project) "
        "AND status = 'active' "
        "AND created_at < datetime('now', '-7 days') "
        "ORDER BY created_at ASC"
    )
    return [_row_to_response(row) async for row in cursor]
