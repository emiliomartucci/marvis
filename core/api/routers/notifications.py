# v1.1.0 - 2026-03-11 - Auto-sync acted_at for task_pending when task approved via Triage
from __future__ import annotations

from datetime import datetime, timezone

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException, Query

from core.api.db import get_db, get_write_db
from core.api.models import UserInfo
from core.api.rbac import require_role

router = APIRouter(prefix="/api/v1/notifications", tags=["notifications"])


@router.get("")
async def list_notifications(
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    unread_only: bool = Query(False),
    user: UserInfo = Depends(require_role("admin", "super_admin", human_only=True)),
    db: aiosqlite.Connection = Depends(get_db),
) -> list[dict]:
    """List notifications for the current user, newest first.

    Read-only: auto-sync of task_pending acted_at runs in a background task
    elsewhere, NOT inline (to avoid SQLITE_BUSY on concurrent ingest).
    """
    db.row_factory = aiosqlite.Row

    ws = user.workspace_id or "ws_default"
    query = "SELECT * FROM notifications WHERE user_id = ? AND COALESCE(workspace_id, 'ws_default') = ?"
    params: list = [user.user_id, ws]

    if unread_only:
        query += " AND read_at IS NULL"

    query += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])

    cursor = await db.execute(query, params)
    rows = await cursor.fetchall()
    return [dict(row) for row in rows]


@router.get("/unread-count")
async def unread_count(
    user: UserInfo = Depends(require_role("admin", "super_admin", human_only=True)),
    db: aiosqlite.Connection = Depends(get_db),
) -> dict:
    """Get the count of unread notifications for badge display."""
    db.row_factory = aiosqlite.Row
    ws = user.workspace_id or "ws_default"
    cursor = await db.execute(
        "SELECT COUNT(*) as count FROM notifications WHERE user_id = ? AND read_at IS NULL AND COALESCE(workspace_id, 'ws_default') = ?",
        (user.user_id, ws),
    )
    row = await cursor.fetchone()
    return {"count": row["count"] if row else 0}


@router.patch("/{notification_id}/read")
async def mark_read(
    notification_id: str,
    user: UserInfo = Depends(require_role("admin", "super_admin", human_only=True)),
    db: aiosqlite.Connection = Depends(get_write_db),
) -> dict:
    """Mark a single notification as read."""
    now = datetime.now(timezone.utc).isoformat()

    # Verify ownership
    db.row_factory = aiosqlite.Row
    cursor = await db.execute(
        "SELECT id FROM notifications WHERE id = ? AND user_id = ?",
        (notification_id, user.user_id),
    )
    if not await cursor.fetchone():
        raise HTTPException(status_code=404, detail="Notification not found")

    await db.execute(
        "UPDATE notifications SET read_at = ? WHERE id = ? AND read_at IS NULL",
        (now, notification_id),
    )
    await db.commit()
    return {"ok": True}


@router.patch("/{notification_id}/acted")
async def mark_acted(
    notification_id: str,
    user: UserInfo = Depends(require_role("admin", "super_admin", human_only=True)),
    db: aiosqlite.Connection = Depends(get_write_db),
) -> dict:
    """Mark a notification as acted upon (approve/reject done)."""
    now = datetime.now(timezone.utc).isoformat()

    db.row_factory = aiosqlite.Row
    cursor = await db.execute(
        "SELECT id FROM notifications WHERE id = ? AND user_id = ?",
        (notification_id, user.user_id),
    )
    if not await cursor.fetchone():
        raise HTTPException(status_code=404, detail="Notification not found")

    await db.execute(
        "UPDATE notifications SET acted_at = ?, read_at = COALESCE(read_at, ?) WHERE id = ?",
        (now, now, notification_id),
    )
    await db.commit()
    return {"ok": True}


@router.post("/mark-all-read")
async def mark_all_read(
    user: UserInfo = Depends(require_role("admin", "super_admin", human_only=True)),
    db: aiosqlite.Connection = Depends(get_write_db),
) -> dict:
    """Mark all notifications as read for the current user."""
    now = datetime.now(timezone.utc).isoformat()
    await db.execute(
        "UPDATE notifications SET read_at = ? WHERE user_id = ? AND read_at IS NULL",
        (now, user.user_id),
    )
    await db.commit()
    return {"ok": True}
