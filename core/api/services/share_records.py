"""FastAPI-free persistence for expiring file-share records."""
from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta

import aiosqlite

from core.api.use_cases._errors import ValidationError


async def create_shared_link_record(
    *,
    stored_path: str,
    public_path: str,
    created_by: str,
    db: aiosqlite.Connection,
    hours: int | float,
    public_url_prefix: str = "/api/v1/shared",
) -> dict[str, str]:
    """Insert one expiring share token without importing an HTTP transport."""
    if (
        isinstance(hours, bool)
        or not isinstance(hours, (int, float))
        or hours <= 0
        or hours > 720
    ):
        raise ValidationError(
            code="invalid_share_expiry",
            message="hours must be between 1 and 720",
        )
    creator = (created_by or "").strip()
    if not creator:
        raise ValidationError(
            code="share_creator_missing",
            message="A persisted caller identity is required",
        )

    token = secrets.token_urlsafe(32)
    expires_at = datetime.now(UTC) + timedelta(hours=hours)
    await db.execute(
        "INSERT INTO shared_links (token, path, created_by, expires_at)"
        " VALUES (?, ?, ?, ?)",
        (token, stored_path, creator, expires_at.isoformat()),
    )
    await db.commit()
    return {
        "token": token,
        "url": f"{public_url_prefix.rstrip('/')}/{token}",
        "expires_at": expires_at.isoformat(),
        "path": public_path,
    }
