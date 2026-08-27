# v1.0.0 - 2026-03-13 - Push subscription management (POST/DELETE)
from __future__ import annotations

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from core.api.db import get_write_db
from core.api.models import UserInfo
from core.api.rbac import require_role
from core.api.services.push_service import MAX_SUBSCRIPTIONS_PER_USER, validate_push_endpoint

router = APIRouter(prefix="/api/v1/push-subscriptions", tags=["push"])


class SubscribeRequest(BaseModel):
    endpoint: str
    p256dh: str
    auth: str


class UnsubscribeRequest(BaseModel):
    endpoint: str


@router.post("", status_code=201)
async def subscribe(
    body: SubscribeRequest,
    user: UserInfo = Depends(require_role("operator", human_only=True)),
    db: aiosqlite.Connection = Depends(get_write_db),
) -> dict:
    """Register a push subscription for the current user."""
    if not validate_push_endpoint(body.endpoint):
        raise HTTPException(400, "Invalid push endpoint")

    # Cap subscriptions per user
    cursor = await db.execute(
        "SELECT COUNT(*) FROM push_subscriptions WHERE user_id = ?",
        [user.user_id],
    )
    row = await cursor.fetchone()
    if row[0] >= MAX_SUBSCRIPTIONS_PER_USER:
        raise HTTPException(400, f"Max {MAX_SUBSCRIPTIONS_PER_USER} subscriptions per user")

    await db.execute(
        "INSERT OR REPLACE INTO push_subscriptions (endpoint, user_id, p256dh, auth) VALUES (?, ?, ?, ?)",
        [body.endpoint, user.user_id, body.p256dh, body.auth],
    )
    await db.commit()
    return {"status": "subscribed"}


@router.delete("")
async def unsubscribe(
    body: UnsubscribeRequest,
    user: UserInfo = Depends(require_role("operator", human_only=True)),
    db: aiosqlite.Connection = Depends(get_write_db),
) -> dict:
    """Remove a push subscription for the current user."""
    result = await db.execute(
        "DELETE FROM push_subscriptions WHERE endpoint = ? AND user_id = ?",
        [body.endpoint, user.user_id],
    )
    await db.commit()
    if result.rowcount == 0:
        raise HTTPException(404, "Subscription not found")
    return {"status": "unsubscribed"}
