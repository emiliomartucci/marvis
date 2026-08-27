# v1.0.0 - 2026-07-03 - P1 F1: per-user notifications use_cases (list + ack), fastapi-free
"""Notifications use_cases — transport-agnostic list/ack for the per-user inbox.

Called by the MCP tools (``core/api/mcp/tools/notifications.py``) and reusable by
the REST router. The security boundary is ``effective_user_id`` (a person's
``users.id``, resolved by the caller via ``person_user_id`` / admin override) plus
the read-time visibility filter: a row whose ``project`` is no longer in the
caller's ``visible_projects`` is neither returned NOR counted, so a revoked grant
takes its stale notifications — and the counter that would otherwise reveal their
existence — with it. Company/program-scope brain notifications (``project IS NULL``
with a brain type) are admin-only for the same reason.
"""
from __future__ import annotations

from datetime import datetime, timezone

import aiosqlite

from core.api.use_cases._context import (
    CallerContext,
    require_role_ctx,
    require_workspace_ctx,
)
from core.api.use_cases._errors import NotFoundError, ValidationError

#: Notification types the brain producer (F3) emits. Company/program-scope ones
#: (``project IS NULL``) are admin-only — a non-admin must never see or count them.
BRAIN_NOTIFICATION_TYPES: tuple[str, ...] = ("brain_finding", "brain_drift")

#: The actionable "things to close" surfaced as `notices` in the entry tools (F4):
#: cross-agent comments + brain findings/drift. The legacy admin-toast types
#: (task_pending, pr_submitted, deploy_*) are intentionally NOT notices.
NOTICE_TYPES: tuple[str, ...] = ("comment", "brain_finding", "brain_drift")

#: Columns returned to callers (never leak workspace_id / event_id internals).
_SELECT_COLUMNS = (
    "id, type, title, body, target_type, target_id, project, "
    "read_at, acted_at, created_at, rollup_count"
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _visibility_clause(visible_projects: set[str] | None) -> tuple[str, list[str]]:
    """SQL fragment + params applying the read-time visibility filter.

    ``visible_projects is None`` → caller is unrestricted (admin/bearer): no clause.
    Otherwise a row is visible only when its project is in the set, OR it is a
    project-less NON-brain personal row. Company-scope brain rows and rows for
    projects no longer granted are dropped (from the list AND, by reuse, the count).
    """
    if visible_projects is None:
        return "", []
    brain_ph = ",".join("?" for _ in BRAIN_NOTIFICATION_TYPES)
    if visible_projects:
        proj_ph = ",".join("?" for _ in visible_projects)
        clause = (
            f"((project IS NULL AND type NOT IN ({brain_ph})) "
            f"OR project IN ({proj_ph}))"
        )
        params = [*BRAIN_NOTIFICATION_TYPES, *sorted(visible_projects)]
    else:
        clause = f"(project IS NULL AND type NOT IN ({brain_ph}))"
        params = [*BRAIN_NOTIFICATION_TYPES]
    return clause, params


async def _require_effective_user(
    ctx: CallerContext,
    db: aiosqlite.Connection,
    effective_user_id: str,
) -> str:
    """Return exact workspace after proving the person belongs to the caller.

    Admin inbox overrides remain useful inside a tenant, but an admin id plus a
    globally valid foreign ``users.id`` must not become a cross-workspace oracle.
    Missing/partial schemas fail closed as the same non-enumerating 404.
    """
    workspace_id = require_workspace_ctx(ctx)
    try:
        row = await (
            await db.execute(
                "SELECT workspace_id FROM users WHERE id = ? "
                "AND type = 'human' AND deleted_at IS NULL",
                (effective_user_id,),
            )
        ).fetchone()
    except aiosqlite.Error:
        row = None
    if row is None:
        raise NotFoundError(code="notification_user_not_found", message="Not found")
    actual_workspace = str(row[0] or "").strip()
    if actual_workspace == workspace_id:
        return workspace_id
    if not actual_workspace:
        try:
            owners = {
                str(owner[0])
                for owner in await (
                    await db.execute("SELECT id FROM workspaces")
                ).fetchall()
                if owner[0]
            }
        except aiosqlite.Error:
            owners = set()
        if owners == {workspace_id}:
            return workspace_id
    raise NotFoundError(code="notification_user_not_found", message="Not found")


async def list_notifications(
    ctx: CallerContext,
    db: aiosqlite.Connection,
    *,
    effective_user_id: str | None,
    visible_projects: set[str] | None,
    status: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> list[dict]:
    """List the caller's notifications, newest first, visibility-filtered.

    ``effective_user_id is None`` (bearer/agent/non-person) → empty list.
    ``status='unread'`` restricts to ``read_at IS NULL``; anything else = all.
    """
    require_role_ctx(ctx, "viewer", "operator", "admin", "super_admin")
    if not effective_user_id:
        return []
    workspace_id = await _require_effective_user(ctx, db, effective_user_id)

    db.row_factory = aiosqlite.Row
    where = ["user_id = ?", "workspace_id = ?"]
    params: list = [effective_user_id, workspace_id]
    if status == "unread":
        where.append("read_at IS NULL")
    vis_clause, vis_params = _visibility_clause(visible_projects)
    if vis_clause:
        where.append(vis_clause)
        params.extend(vis_params)

    query = (
        f"SELECT {_SELECT_COLUMNS} FROM notifications "
        f"WHERE {' AND '.join(where)} "
        "ORDER BY created_at DESC LIMIT ? OFFSET ?"
    )
    params.extend([max(1, min(int(limit), 200)), max(0, int(offset))])
    rows = await (await db.execute(query, params)).fetchall()
    return [dict(row) for row in rows]


async def ack_notification(
    ctx: CallerContext,
    db: aiosqlite.Connection,
    *,
    effective_user_id: str | None,
    notification_id: str | None = None,
    target_id: str | None = None,
) -> dict:
    """Mark the caller's notification(s) as read (dismiss). Returns ``{acked: n}``.

    Exactly one of ``notification_id`` or ``target_id`` must be given. Only the
    caller's OWN unread rows are touched (``user_id = ?``), so acking another
    user's id/target is a silent no-op (``acked: 0``), never a cross-user write.
    Sets ``read_at`` only — the notice counter (F4) keys on ``read_at IS NULL``, so
    an ack makes the notice disappear; ``acted_at`` (the task_pending badge axis)
    is left untouched.
    """
    require_role_ctx(ctx, "viewer", "operator", "admin", "super_admin")
    if not effective_user_id:
        return {"acked": 0}
    workspace_id = await _require_effective_user(ctx, db, effective_user_id)
    if bool(notification_id) == bool(target_id):
        raise ValidationError(
            code="ack_needs_one_target",
            message="Provide exactly one of notification_id or target_id.",
        )

    now = _now()
    if notification_id:
        cur = await db.execute(
            "UPDATE notifications SET read_at = ? "
            "WHERE id = ? AND user_id = ? AND workspace_id = ? "
            "AND read_at IS NULL",
            (now, notification_id, effective_user_id, workspace_id),
        )
    else:
        cur = await db.execute(
            "UPDATE notifications SET read_at = ? "
            "WHERE user_id = ? AND workspace_id = ? AND target_id = ? "
            "AND read_at IS NULL",
            (now, effective_user_id, workspace_id, target_id),
        )
    await db.commit()
    return {"acked": cur.rowcount or 0}


async def count_unread_notices(
    ctx: CallerContext,
    db: aiosqlite.Connection,
    *,
    effective_user_id: str | None,
    visible_projects: set[str] | None,
    project: str | None = None,
    task: str | None = None,
) -> dict[str, int]:
    """Unread NOTICE_TYPES counts by type for the entry-tool `notices` field (F4).

    Same read-time visibility filter as ``list_notifications`` (a revoked-grant
    project and company-scope brain are dropped from the count too, so the counter
    can never reveal the existence of something the caller may no longer see).
    ``task`` scopes to that task's target; ``project`` scopes to that project plus
    the caller's project-less personal notices. Index-only on the partial unread
    index. Returns ``{}`` for a non-person caller or when nothing is unread.
    """
    if not effective_user_id:
        return {}
    workspace_id = await _require_effective_user(ctx, db, effective_user_id)
    db.row_factory = aiosqlite.Row
    type_ph = ",".join("?" for _ in NOTICE_TYPES)
    where = [
        "user_id = ?",
        "workspace_id = ?",
        "read_at IS NULL",
        f"type IN ({type_ph})",
    ]
    params: list = [effective_user_id, workspace_id, *NOTICE_TYPES]
    if task is not None:
        where.append("target_type = 'task' AND target_id = ?")
        params.append(task)
    elif project is not None:
        where.append("(project = ? OR project IS NULL)")
        params.append(project)
    vis_clause, vis_params = _visibility_clause(visible_projects)
    if vis_clause:
        where.append(vis_clause)
        params.extend(vis_params)
    query = (
        f"SELECT type, COUNT(*) AS n FROM notifications WHERE {' AND '.join(where)} GROUP BY type"
    )
    rows = await (await db.execute(query, params)).fetchall()
    return {row["type"]: row["n"] for row in rows if row["n"]}


async def count_unread_notifications(
    ctx: CallerContext,
    db: aiosqlite.Connection,
    *,
    effective_user_id: str | None,
    visible_projects: set[str] | None,
) -> int:
    """Count the caller's exact-workspace unread inbox after visibility filtering."""
    require_role_ctx(ctx, "viewer", "operator", "admin", "super_admin")
    if not effective_user_id:
        return 0
    workspace_id = await _require_effective_user(ctx, db, effective_user_id)
    where = ["user_id = ?", "workspace_id = ?", "read_at IS NULL"]
    params: list = [effective_user_id, workspace_id]
    vis_clause, vis_params = _visibility_clause(visible_projects)
    if vis_clause:
        where.append(vis_clause)
        params.extend(vis_params)
    row = await (
        await db.execute(
            f"SELECT COUNT(*) FROM notifications WHERE {' AND '.join(where)}",
            params,
        )
    ).fetchone()
    return int(row[0]) if row else 0


async def mark_notification(
    ctx: CallerContext,
    db: aiosqlite.Connection,
    *,
    effective_user_id: str | None,
    notification_id: str,
    acted: bool = False,
) -> dict[str, bool]:
    """Mark one exact-workspace notification read/acted or return a generic 404."""
    require_role_ctx(ctx, "viewer", "operator", "admin", "super_admin")
    if not effective_user_id:
        raise NotFoundError(
            code="notification_not_found", message="Notification not found"
        )
    workspace_id = await _require_effective_user(ctx, db, effective_user_id)
    now = _now()
    if acted:
        cursor = await db.execute(
            "UPDATE notifications SET acted_at = ?, read_at = COALESCE(read_at, ?) "
            "WHERE id = ? AND user_id = ? AND workspace_id = ?",
            (now, now, notification_id, effective_user_id, workspace_id),
        )
    else:
        cursor = await db.execute(
            "UPDATE notifications SET read_at = COALESCE(read_at, ?) "
            "WHERE id = ? AND user_id = ? AND workspace_id = ?",
            (now, notification_id, effective_user_id, workspace_id),
        )
    if cursor.rowcount != 1:
        await db.rollback()
        raise NotFoundError(code="notification_not_found", message="Notification not found")
    await db.commit()
    return {"ok": True}


async def mark_all_read(
    ctx: CallerContext,
    db: aiosqlite.Connection,
    *,
    effective_user_id: str | None,
) -> dict[str, bool]:
    """Mark only the caller's exact-workspace unread rows."""
    require_role_ctx(ctx, "viewer", "operator", "admin", "super_admin")
    if not effective_user_id:
        return {"ok": True}
    workspace_id = await _require_effective_user(ctx, db, effective_user_id)
    await db.execute(
        "UPDATE notifications SET read_at = ? "
        "WHERE user_id = ? AND workspace_id = ? AND read_at IS NULL",
        (_now(), effective_user_id, workspace_id),
    )
    await db.commit()
    return {"ok": True}
