# v1.0.0 - 2026-04-13 - Admin CRUD for app_settings table
"""Admin endpoints for managing app_settings key-value store.

The app_settings table is used for runtime feature flags, kill switches,
budget caps, and other configuration that changes without a redeploy.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from core.api.db import get_db, get_write_db
from core.api.models import UserInfo
from core.api.rbac import require_role
from core.api.security import get_current_user_or_agent

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/admin/app-settings", tags=["admin"])


class AppSettingResponse(BaseModel):
    key: str
    value: str
    updated_at: str | None = None


class AppSettingUpsertRequest(BaseModel):
    value: str = Field(..., max_length=10000)


@router.get("", response_model=list[AppSettingResponse])
async def list_app_settings(
    user: UserInfo = Depends(get_current_user_or_agent),
    db: aiosqlite.Connection = Depends(get_db),
) -> list[AppSettingResponse]:
    """List all app settings. Requires operator+ role."""
    cursor = await db.execute(
        "SELECT key, value, updated_at FROM app_settings ORDER BY key"
    )
    rows = await cursor.fetchall()
    return [
        AppSettingResponse(
            key=row["key"],
            value=row["value"],
            updated_at=row["updated_at"],
        )
        for row in rows
    ]


@router.get("/{key}", response_model=AppSettingResponse)
async def get_app_setting(
    key: str,
    user: UserInfo = Depends(get_current_user_or_agent),
    db: aiosqlite.Connection = Depends(get_db),
) -> AppSettingResponse:
    """Get a single app setting by key. Requires operator+ role."""
    cursor = await db.execute(
        "SELECT key, value, updated_at FROM app_settings WHERE key = ?",
        (key,),
    )
    row = await cursor.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"Setting not found: {key}")
    return AppSettingResponse(
        key=row["key"],
        value=row["value"],
        updated_at=row["updated_at"],
    )


@router.put("/{key}", response_model=AppSettingResponse)
async def upsert_app_setting(
    key: str,
    body: AppSettingUpsertRequest,
    user: UserInfo = Depends(require_role("admin", "super_admin")),
    db: aiosqlite.Connection = Depends(get_write_db),
) -> AppSettingResponse:
    """Create or update an app setting. Admin only."""
    now = datetime.now(timezone.utc).isoformat()
    await db.execute(
        "INSERT INTO app_settings (key, value, updated_at) "
        "VALUES (?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
        (key, body.value, now),
    )
    await db.commit()
    logger.info("App setting upserted: %s by %s", key, user.username)
    return AppSettingResponse(
        key=key,
        value=body.value,
        updated_at=now,
    )


@router.delete("/{key}", status_code=204)
async def delete_app_setting(
    key: str,
    user: UserInfo = Depends(require_role("admin", "super_admin")),
    db: aiosqlite.Connection = Depends(get_write_db),
) -> None:
    """Delete an app setting. Admin only."""
    cursor = await db.execute("DELETE FROM app_settings WHERE key = ?", (key,))
    await db.commit()
    if cursor.rowcount == 0:
        raise HTTPException(status_code=404, detail=f"Setting not found: {key}")
    logger.info("App setting deleted: %s by %s", key, user.username)
