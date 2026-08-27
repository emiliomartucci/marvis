# v1.4.0 - 2026-07-03 - P1 F1: single-writer notify() with per-(user,type,target) rollup
from __future__ import annotations

import logging
from collections.abc import Iterable
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


async def _recipient_workspace(
    db: aiosqlite.Connection,
    user_id: str,
    requested_workspace_id: str | None,
) -> str | None:
    """Resolve one non-empty recipient workspace, never a synthetic default."""
    try:
        row = await (
            await db.execute(
                "SELECT workspace_id FROM users WHERE id = ? AND deleted_at IS NULL",
                (user_id,),
            )
        ).fetchone()
    except aiosqlite.Error:
        return None
    if row is None:
        return None
    actual = str(row[0] or "").strip()
    requested = str(requested_workspace_id or "").strip()
    if not actual:
        try:
            workspaces = {
                str(owner[0])
                for owner in await (
                    await db.execute("SELECT id FROM workspaces")
                ).fetchall()
                if owner[0]
            }
        except aiosqlite.Error:
            return None
        if len(workspaces) != 1:
            return None
        actual = next(iter(workspaces))
    if requested and actual != requested:
        return None
    return requested or actual


async def _resolve_event_workspace(
    db: aiosqlite.Connection,
    *,
    event_id: str | None,
    target_type: str | None,
    target_id: str | None,
    project: str | None,
    workspace_id: str | None,
) -> str | None:
    """Resolve event ownership from exact persisted evidence or fail closed."""
    requested = str(workspace_id or "").strip()
    candidates: set[str] = set()
    try:
        if event_id:
            rows = await (
                await db.execute(
                    "SELECT workspace_id FROM events WHERE id = ?",
                    (event_id,),
                )
            ).fetchall()
            candidates.update(str(row[0]).strip() for row in rows if row[0])
        if not candidates and target_id and target_type in {"task", "pr"}:
            table = "tasks" if target_type == "task" else "pull_requests"
            rows = await (
                await db.execute(
                    f"SELECT workspace_id FROM {table} WHERE id = ?",  # noqa: S608
                    (target_id,),
                )
            ).fetchall()
            candidates.update(str(row[0]).strip() for row in rows if row[0])
        if not candidates and project:
            rows = await (
                await db.execute(
                    "SELECT workspace_id FROM workspace_projects "
                    "WHERE project_slug = ?",
                    (project,),
                )
            ).fetchall()
            candidates.update(str(row[0]).strip() for row in rows if row[0])
    except aiosqlite.Error:
        return None
    if requested:
        return requested if candidates == {requested} else None
    if len(candidates) == 1:
        return next(iter(candidates))
    return None


async def notify(
    db: aiosqlite.Connection,
    *,
    user_ids: Iterable[str],
    type: str,
    title: str,
    target_type: str | None = None,
    target_id: str | None = None,
    project: str | None = None,
    body: str | None = None,
    event_id: str | None = None,
    workspace_id: str | None = None,
    acted_at: str | None = None,
) -> int:
    """Single-writer fan-out (P1 F1): deliver one notification to each recipient.

    THE one place notification rows are created — the three legacy direct writers
    (this service's fan-out, the zombie-scan in use_cases/tasks.py, and the
    user_provisioning poison notifier) all route through here so the delivery and
    anti-spam semantics live in one covenant-owning function.

    Two delivery modes, chosen by whether ``event_id`` is set:

    * ``event_id`` present → **dedup-by-event**. The partial UNIQUE index
      ``(event_id, user_id)`` makes a repeat a no-op, so a brain finding/drift
      signal (which carries its stable id as ``event_id``) notifies each user
      ONCE EVER, not once per cycle. No rollup bump.
    * ``event_id`` absent → **rollup**. Bump ``rollup_count`` on an existing UNREAD
      row for the same ``(user_id, type, target_type, target_id)``; if there is
      none, insert. N comments on one task collapse to a single row with
      ``rollup_count = N`` (the anti-spam decision).

    Best-effort per recipient: a CHECK/constraint failure on one user is LOGGED
    (never swallowed silently — that is the bug the mig-163 CHECK extension fixes)
    and never aborts the others. Does NOT commit — the caller owns the write
    transaction (covenant single-writer). Returns the number of rows touched.
    """
    touched = 0
    seen: set[str] = set()
    for uid in user_ids:
        if not uid or uid in seen:
            continue
        seen.add(uid)
        try:
            recipient_workspace = await _recipient_workspace(db, uid, workspace_id)
            if recipient_workspace is None:
                logger.warning(
                    "notify: recipient workspace mismatch or unavailable for user=%s",
                    uid,
                )
                continue
            if not event_id and target_id is not None:
                # Rollup: fold repeats onto the still-unread row for this target.
                cur = await db.execute(
                    "UPDATE notifications "
                    "SET rollup_count = rollup_count + 1, title = ?, body = ? "
                    "WHERE user_id = ? AND type = ? AND read_at IS NULL "
                    "AND workspace_id = ? "
                    "AND COALESCE(target_type, '') = COALESCE(?, '') "
                    "AND COALESCE(target_id, '') = COALESCE(?, '')",
                    (
                        title,
                        body,
                        uid,
                        type,
                        recipient_workspace,
                        target_type,
                        target_id,
                    ),
                )
                if cur.rowcount and cur.rowcount > 0:
                    touched += cur.rowcount
                    continue
            cur = await db.execute(
                "INSERT OR IGNORE INTO notifications "
                "(user_id, event_id, type, title, body, target_type, target_id, "
                "project, workspace_id, acted_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    uid,
                    event_id,
                    type,
                    title,
                    body,
                    target_type,
                    target_id,
                    project,
                    recipient_workspace,
                    acted_at,
                ),
            )
            touched += cur.rowcount or 0
        except Exception as exc:  # noqa: BLE001 — best-effort per recipient
            logger.warning(
                "notify: delivery failed for user=%s type=%s target=%s/%s: %s",
                uid,
                type,
                target_type,
                target_id,
                exc,
            )
    return touched


async def generate_from_event(
    db: aiosqlite.Connection,
    event_type: str,
    event_id: str | None,
    project: str | None,
    actor_id: str | None,
    target_type: str | None,
    target_id: str | None,
    payload: dict[str, Any] | None = None,
    workspace_id: str | None = None,
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
        resolved_workspace = await _resolve_event_workspace(
            db,
            event_id=event_id,
            target_type=target_type,
            target_id=target_id,
            project=project,
            workspace_id=workspace_id,
        )
        if resolved_workspace is None:
            logger.warning(
                "notification generation skipped: workspace ownership unavailable "
                "for event=%s target=%s/%s",
                event_id,
                target_type,
                target_id,
            )
            return
        db.row_factory = aiosqlite.Row
        query = (
            "SELECT id FROM users "
            "WHERE type = 'human' AND system_role IN ('admin', 'super_admin') "
            "AND workspace_id = ? AND deleted_at IS NULL"
        )
        params: list[str] = [resolved_workspace]
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

        # Route through the single-writer notify(): dedup-by-event when the event
        # carries an id, rollup otherwise. This replaces the old per-recipient
        # INSERT loop + the pr_submitted manual dedup (notify() owns both now).
        await notify(
            db,
            user_ids=[row["id"] for row in recipients],
            type=notif_type,
            title=title,
            body=body,
            target_type=expected_target,
            target_id=target_id,
            project=project,
            event_id=event_id,
            workspace_id=resolved_workspace,
            acted_at=acted_at,
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
