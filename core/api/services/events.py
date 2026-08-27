# v1.4.0 - 2026-03-13 - Add workspace_id param (enterprise multi-tenancy, no ContextVar in background)
from __future__ import annotations

import json
import logging
import uuid
from typing import Any

import aiosqlite

logger = logging.getLogger(__name__)


async def emit_event(
    db: aiosqlite.Connection,
    event_type: str,
    project: str | None = None,
    actor_id: str | None = None,
    target_type: str | None = None,
    target_id: str | None = None,
    payload: dict[str, Any] | None = None,
    workspace_id: str | None = None,
) -> str | None:
    """Emit an event to the events table (transactional outbox).

    Returns the event ID on success, None on failure.
    Non-critical: failures are logged as warnings but never block the caller.

    IMPORTANT: workspace_id must be passed explicitly. Do NOT use ContextVar —
    it doesn't propagate to BackgroundTasks, asyncio.create_task(), or
    other asynchronous handlers. Always pass workspace_id from the request scope.

    NOTE: if used inside a FastAPI BackgroundTask, `db` must be a fresh connection
    opened with aiosqlite.connect(settings.db_path) — never pass the request-scoped
    db dependency here (it will be closed before the background task runs).
    """
    event_id = uuid.uuid4().hex[:32]
    try:
        # Resolve actor_id to real user id (callers may pass slug or id)
        safe_actor_id = None
        if actor_id:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT id FROM users WHERE id = ? OR slug = ?",
                (actor_id, actor_id),
            )
            row = await cur.fetchone()
            if row:
                safe_actor_id = row["id"]

        await db.execute(
            "INSERT INTO events "
            "(id, event_type, project, actor_id, target_type, target_id, payload, workspace_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                event_id,
                event_type,
                project,
                safe_actor_id,
                target_type,
                target_id,
                json.dumps(payload or {}),
                workspace_id,
            ),
        )
        await db.commit()
        return event_id
    except Exception as exc:  # noqa: BLE001
        logger.warning("emit_event failed (non-critical): %s", exc)
        return None
