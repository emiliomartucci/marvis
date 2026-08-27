# v1.2.0 - 2026-07-03 - P1 F1: open the per-user endpoints to any authenticated human (own records)
from __future__ import annotations

import aiosqlite
from fastapi import APIRouter, Depends, Query

from core.api.db import get_db, get_write_db
from core.api.models import UserInfo
from core.api.rbac import require_role
from core.api.routers._adapter import to_http
from core.api.services import access_grants
from core.api.use_cases import notifications as notifications_uc
from core.api.use_cases._context import CallerContext
from core.api.use_cases._errors import ServiceError

router = APIRouter(prefix="/api/v1/notifications", tags=["notifications"])


@router.get("")
async def list_notifications(
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    unread_only: bool = Query(False),
    user: UserInfo = Depends(
        require_role("viewer", "operator", "admin", "super_admin", human_only=True)
    ),
    db: aiosqlite.Connection = Depends(get_db),
) -> list[dict]:
    """List notifications for the current user, newest first.

    Read-only: auto-sync of task_pending acted_at runs in a background task
    elsewhere, NOT inline (to avoid SQLITE_BUSY on concurrent ingest).
    """
    ctx = CallerContext.from_user_info(user, is_human_session=True)
    try:
        visible = await access_grants.visible_projects_for_actor(db, ctx)
        return await notifications_uc.list_notifications(
            ctx,
            db,
            effective_user_id=user.user_id,
            visible_projects=visible,
            status="unread" if unread_only else None,
            limit=limit,
            offset=offset,
        )
    except ServiceError as e:
        raise to_http(e)


@router.get("/unread-count")
async def unread_count(
    user: UserInfo = Depends(
        require_role("viewer", "operator", "admin", "super_admin", human_only=True)
    ),
    db: aiosqlite.Connection = Depends(get_db),
) -> dict:
    """Get the count of unread notifications for badge display."""
    ctx = CallerContext.from_user_info(user, is_human_session=True)
    try:
        visible = await access_grants.visible_projects_for_actor(db, ctx)
        count = await notifications_uc.count_unread_notifications(
            ctx,
            db,
            effective_user_id=user.user_id,
            visible_projects=visible,
        )
        return {"count": count}
    except ServiceError as e:
        raise to_http(e)


@router.patch("/{notification_id}/read")
async def mark_read(
    notification_id: str,
    user: UserInfo = Depends(
        require_role("viewer", "operator", "admin", "super_admin", human_only=True)
    ),
    db: aiosqlite.Connection = Depends(get_write_db),
) -> dict:
    """Mark a single notification as read."""
    ctx = CallerContext.from_user_info(user, is_human_session=True)
    try:
        return await notifications_uc.mark_notification(
            ctx,
            db,
            effective_user_id=user.user_id,
            notification_id=notification_id,
        )
    except ServiceError as e:
        raise to_http(e)


@router.patch("/{notification_id}/acted")
async def mark_acted(
    notification_id: str,
    user: UserInfo = Depends(
        require_role("viewer", "operator", "admin", "super_admin", human_only=True)
    ),
    db: aiosqlite.Connection = Depends(get_write_db),
) -> dict:
    """Mark a notification as acted upon (approve/reject done)."""
    ctx = CallerContext.from_user_info(user, is_human_session=True)
    try:
        return await notifications_uc.mark_notification(
            ctx,
            db,
            effective_user_id=user.user_id,
            notification_id=notification_id,
            acted=True,
        )
    except ServiceError as e:
        raise to_http(e)


@router.post("/mark-all-read")
async def mark_all_read(
    user: UserInfo = Depends(
        require_role("viewer", "operator", "admin", "super_admin", human_only=True)
    ),
    db: aiosqlite.Connection = Depends(get_write_db),
) -> dict:
    """Mark all notifications as read for the current user."""
    ctx = CallerContext.from_user_info(user, is_human_session=True)
    try:
        return await notifications_uc.mark_all_read(
            ctx,
            db,
            effective_user_id=user.user_id,
        )
    except ServiceError as e:
        raise to_http(e)
