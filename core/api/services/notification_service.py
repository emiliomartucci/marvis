# v1.3.0 - 2026-03-20 - Fix duplicate toast: use task_auto_approved type
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import aiosqlite

logger = logging.getLogger(__name__)

# Maps event_type -> (notification_type, target_type)
EVENT_TO_NOTIFICATION: dict[str, tuple[str, str]] = {
    "task.created": ("task_pending", "task"),
    "task.status_changed": ("task_completed", "task"),
    "pr.submitted": ("pr_submitted", "pr"),
    "deploy.failed": ("deploy_failed", "pr"),
    "deploy.success": ("deploy_success", "pr"),
}


async def generate_from_event(
    db: aiosqlite.Connection,
    event_type: str,
    event_id: str | None,
    project: str | None,
    actor_id: str | None,
    target_type: str | None,
    target_id: str | None,
    payload: dict[str, Any] | None = None,
) -> None:
    """Generate notification rows for relevant users.

    Non-critical: failures are logged as warnings but never block the caller.
    """
    payload = payload or {}

    mapping = EVENT_TO_NOTIFICATION.get(event_type)
    if not mapping:
        return

    notif_type, expected_target = mapping

    # For task.status_changed, only generate notification on completion
    if event_type == "task.status_changed" and payload.get("new_status") != "completed":
        return

    title, body = _build_content(notif_type, payload)

    try:
        db.row_factory = aiosqlite.Row
        query = (
            "SELECT id FROM users "
            "WHERE type = 'human' AND system_role IN ('admin', 'super_admin')"
        )
        params: list[str] = []
        if actor_id:
            query += " AND id != ? AND slug != ?"
            params.extend([actor_id, actor_id])

        cursor = await db.execute(query, params)
        recipients = await cursor.fetchall()
        logger.info(
            "notif: %d recipients for event %s (actor=%s)",
            len(recipients),
            event_id,
            actor_id,
        )

        # Auto-approved tasks: use different type so frontend shows badge, not buttons
        acted_at = None
        if notif_type == "task_pending" and payload.get("status") == "approved":
            notif_type = "task_auto_approved"
            acted_at = datetime.now(timezone.utc).isoformat()
            title = "Task auto-approved"

        for user_row in recipients:
            if notif_type == "pr_submitted" and target_id:
                existing_cursor = await db.execute(
                    "SELECT id FROM notifications WHERE user_id = ? AND type = ? AND target_type = ? AND target_id = ? LIMIT 1",
                    (user_row["id"], notif_type, expected_target, target_id),
                )
                if await existing_cursor.fetchone():
                    continue
            await db.execute(
                "INSERT OR IGNORE INTO notifications "
                "(user_id, event_id, type, title, body, target_type, target_id, project, acted_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    user_row["id"],
                    event_id,
                    notif_type,
                    title,
                    body,
                    expected_target,
                    target_id,
                    project,
                    acted_at,
                ),
            )

        await db.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning("notification generation failed for event %s: %s", event_id, exc)


def _build_content(notif_type: str, payload: dict[str, Any]) -> tuple[str, str | None]:
    """Build notification title and body from type and payload."""
    if notif_type == "task_pending":
        parts = [payload.get("title", "")]
        meta: list[str] = []
        if payload.get("created_by"):
            meta.append(f"by {payload['created_by']}")
        if payload.get("delegation"):
            meta.append(payload["delegation"])
        ice_parts = []
        for k in ("impact", "confidence", "ease"):
            v = payload.get(k)
            if v is not None:
                ice_parts.append(f"{k[0].upper()}{v}")
        if ice_parts:
            meta.append(" ".join(ice_parts))
        if meta:
            parts.append(" | ".join(meta))
        return "New task pending", " — ".join(p for p in parts if p)
    elif notif_type == "pr_submitted":
        return "PR submitted for review", payload.get("branch") or payload.get("title")
    elif notif_type == "task_completed":
        return "Task completed", payload.get("title")
    elif notif_type == "deploy_failed":
        return "Deploy failed", payload.get("output", "")[:200]
    elif notif_type == "deploy_success":
        return "Deploy successful", payload.get("project") or "unknown"
    return notif_type, None
