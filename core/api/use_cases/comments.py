# v1.0.0 - 2026-07-03 - P1 F2: shared comments use_cases (create + list), fastapi-free
"""Comments use_cases — the ONE create/list implementation shared by the MCP tools
(``comment_task`` / ``list_comments``) and the REST router.

The security + hygiene rules live HERE so neither surface can drift:

* **RBAC at read AND write**: the comment's governing project must be in the
  caller's ``visible_projects`` (``None`` = admin, unrestricted). A caller without
  the grant gets a 404-class ``NotFoundError`` — never a 403 that would leak the
  target's existence. This closes the pre-existing REST gap as a by-product.
* **Redaction**: every body is run through the bug_reports whitelist redactor
  BEFORE insert, so a pasted secret never reaches a reader or the embeddings.
* **On-insert notify**: the single-writer ``notify()`` delivers to the task owner
  plus the distinct human thread authors (minus the actor), rolled up per (user,
  task). A non-person owner is logged as an orphan and falls back to workspace
  admins — never a silent drop.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import aiosqlite

from core.api.models.common import CommentReaction, CommentResponse
from core.api.services import access_grants, bug_reports_core
from core.api.use_cases._context import (
    CallerContext,
    require_role_ctx,
    require_workspace_ctx,
)
from core.api.use_cases._errors import (
    AuthorizationError,
    ConflictError,
    NotFoundError,
    ValidationError,
)

logger = logging.getLogger(__name__)

COMMENT_BODY_CAP = 5000

_TASK_NOT_FOUND = "target_not_found"
_TASK_NOT_FOUND_MSG = "Target not found or not visible to you."


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _governing_project(
    db: aiosqlite.Connection,
    ctx: CallerContext,
    target_type: str,
    target_id: str,
) -> tuple[bool, str | None, str | None, str]:
    """(exists, project, owner_id, workspace_id) for RBAC.

    ``comments`` has no workspace column. Task comments remain isolatable through
    the workspace-owned task id. Project comments are safe only while exactly one
    workspace owns the slug. Program comments have no durable workspace binding,
    so only the trusted local single-user identity may use them.
    """
    workspace_id = require_workspace_ctx(ctx)
    if target_type == "task":
        try:
            row = await (
                await db.execute(
                    "SELECT project, owner_id FROM tasks "
                    "WHERE id = ? AND workspace_id = ? AND deleted_at IS NULL",
                    (target_id, workspace_id),
                )
            ).fetchone()
        except aiosqlite.Error:
            row = None
        if row is None:
            return False, None, None, workspace_id
        return True, row[0], row[1], workspace_id
    if target_type == "project":
        if not ctx.is_local_os_account:
            try:
                owners = {
                    str(row[0])
                    for row in await (
                        await db.execute(
                            "SELECT workspace_id FROM workspace_projects "
                            "WHERE project_slug = ?",
                            (target_id,),
                        )
                    ).fetchall()
                    if row[0]
                }
            except aiosqlite.Error:
                owners = set()
            if owners != {workspace_id}:
                return False, None, None, workspace_id
        return True, target_id, None, workspace_id
    if target_type == "program":
        if ctx.is_local_os_account:
            return True, None, None, workspace_id
        return False, None, None, workspace_id
    return False, None, None, workspace_id


async def _authorize(
    db: aiosqlite.Connection, ctx: CallerContext, target_type: str, target_id: str
) -> tuple[str | None, str | None, str]:
    """Return (project, owner_id, workspace_id) or a non-enumerating 404."""
    exists, project, owner_id, workspace_id = await _governing_project(
        db, ctx, target_type, target_id
    )
    visible = await access_grants.visible_projects_for_actor(db, ctx)  # None = admin
    if not exists:
        raise NotFoundError(code=_TASK_NOT_FOUND, message=_TASK_NOT_FOUND_MSG)
    if visible is None:
        return project, owner_id, workspace_id
    # non-admin: program-scope (project None) is not theirs, and a project not in
    # their visible set is treated as non-existent (no existence leak).
    if project is None or project not in visible:
        raise NotFoundError(code=_TASK_NOT_FOUND, message=_TASK_NOT_FOUND_MSG)
    return project, owner_id, workspace_id


def _parse_reactions(raw: str | None) -> list[CommentReaction]:
    if not raw:
        return []
    out: list[CommentReaction] = []
    for part in raw.split("|"):
        pieces = part.split(":", 1)
        if len(pieces) == 2:
            out.append(CommentReaction(reaction=pieces[0], created_by=pieces[1]))
    return out


async def _notify_comment(
    db: aiosqlite.Connection,
    *,
    target_type: str,
    target_id: str,
    project: str | None,
    owner_id: str | None,
    workspace_id: str,
    actor: CallerContext,
    snippet: str,
) -> None:
    """Deliver the on-insert notification to owner + distinct human thread authors."""
    from core.api.services.notification_service import notify

    recipients: set[str] = set()
    # distinct human thread authors. created_by is stored as ctx.username: a
    # users.slug for seeded agents, but the WorkOS sub (== users.id) for OAuth
    # persons (sync_oauth_user lowercases the slug / derives it from email), so
    # match EITHER slug or id — matching slug alone silently drops OAuth persons.
    rows = await (
        await db.execute(
            "SELECT DISTINCT u.id FROM comments c JOIN users u "
            "ON (u.slug = c.created_by OR u.id = c.created_by) "
            "WHERE c.target_type = ? AND c.target_id = ? AND c.deleted_at IS NULL "
            "AND u.type = 'human' AND u.deleted_at IS NULL "
            "AND u.workspace_id = ?",
            (target_type, target_id, workspace_id),
        )
    ).fetchall()
    recipients.update(r[0] for r in rows)

    if target_type == "task" and owner_id:
        orow = await (
            await db.execute(
                "SELECT type FROM users WHERE id = ? AND workspace_id = ? "
                "AND deleted_at IS NULL",
                (owner_id, workspace_id),
            )
        ).fetchone()
        if orow is not None and orow[0] == "human":
            recipients.add(owner_id)
        else:
            # Orphan: owner is a non-person identity -> never a pure drop.
            logger.warning(
                "orphan_notification_target: task=%s owner_id=%s not a person; "
                "falling back to workspace admins",
                target_id,
                owner_id,
            )
            arows = await (
                await db.execute(
                    "SELECT id FROM users WHERE type = 'human' "
                    "AND system_role IN ('admin', 'super_admin') "
                    "AND workspace_id = ? AND deleted_at IS NULL",
                    (workspace_id,),
                )
            ).fetchall()
            recipients.update(a[0] for a in arows)

    # never self-notify the actor
    if actor.user_id:
        recipients.discard(actor.user_id)
    if not recipients:
        return
    await notify(
        db,
        user_ids=list(recipients),
        type="comment",
        title="Nuovo commento su un task" if target_type == "task" else "Nuovo commento",
        body=snippet[:500],
        target_type="task" if target_type == "task" else None,
        target_id=target_id if target_type == "task" else None,
        project=project,
        workspace_id=workspace_id,
    )


async def create_comment(
    ctx: CallerContext,
    db: aiosqlite.Connection,
    *,
    target_type: str,
    target_id: str,
    body: str,
    status: str = "info",
    parent_id: int | None = None,
) -> CommentResponse:
    """Create a comment (RBAC + redaction + notify inside). Commits."""
    require_role_ctx(ctx, "operator", "admin", "super_admin")
    project, owner_id, workspace_id = await _authorize(
        db, ctx, target_type, target_id
    )

    red_body, _ = bug_reports_core.redact(bug_reports_core.cap(body, COMMENT_BODY_CAP))
    if not red_body.strip():
        raise ValidationError(
            code="empty_comment", message="Comment body is empty after redaction."
        )

    now = _now()
    if parent_id is not None:
        parent = await (
            await db.execute(
                "SELECT target_type, target_id, parent_id FROM comments "
                "WHERE id = ? AND deleted_at IS NULL",
                (parent_id,),
            )
        ).fetchone()
        if (
            parent is None
            or parent[0] != target_type
            or parent[1] != target_id
            or parent[2] is not None
        ):
            raise ValidationError(
                code="invalid_parent",
                message="Parent comment is not available for this target.",
            )
    try:
        cur = await db.execute(
            "INSERT INTO comments (target_type, target_id, body, status, created_by, created_at, parent_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (target_type, target_id, red_body, status, ctx.username, now, parent_id),
        )
    except aiosqlite.IntegrityError as exc:
        msg = str(exc).lower()
        if "max depth" in msg:
            raise ValidationError(
                code="max_depth", message="Cannot reply to a reply (max depth 1)."
            )
        raise ValidationError(code="integrity_error", message=str(exc))

    comment_id = cur.lastrowid
    await _notify_comment(
        db,
        target_type=target_type,
        target_id=target_id,
        project=project,
        owner_id=owner_id,
        workspace_id=workspace_id,
        actor=ctx,
        snippet=red_body,
    )
    await db.commit()
    return CommentResponse(
        id=comment_id,
        target_type=target_type,
        target_id=target_id,
        body=red_body,
        status=status,
        created_by=ctx.username,
        created_at=now,
        edited_at=None,
        parent_id=parent_id,
        reactions=[],
        replies=[],
    )


async def list_comments(
    ctx: CallerContext,
    db: aiosqlite.Connection,
    *,
    target_type: str,
    target_id: str,
    status: str | None = None,
    created_by: str | None = None,
    limit: int = 200,
    offset: int = 0,
) -> list[CommentResponse]:
    """List comments (threaded, with reactions) after the RBAC 404 gate."""
    require_role_ctx(ctx, "viewer", "operator", "admin", "super_admin")
    await _authorize(db, ctx, target_type, target_id)

    db.row_factory = aiosqlite.Row
    query = (
        "SELECT c.*, GROUP_CONCAT(cr.reaction || ':' || cr.created_by, '|') AS reactions_raw "
        "FROM comments c LEFT JOIN comment_reactions cr ON cr.comment_id = c.id "
        "WHERE c.target_type = ? AND c.target_id = ? AND c.deleted_at IS NULL"
    )
    params: list = [target_type, target_id]
    if status:
        query += " AND c.status = ?"
        params.append(status)
    if created_by:
        query += " AND c.created_by = ?"
        params.append(created_by)
    query += " GROUP BY c.id ORDER BY c.created_at ASC LIMIT ? OFFSET ?"
    params.extend([max(1, min(int(limit), 500)), max(0, int(offset))])

    rows = await (await db.execute(query, params)).fetchall()
    by_id: dict[int, CommentResponse] = {}
    top: list[CommentResponse] = []
    for row in rows:
        c = CommentResponse(
            id=row["id"],
            target_type=row["target_type"],
            target_id=row["target_id"],
            body=row["body"],
            status=row["status"],
            created_by=row["created_by"],
            created_at=row["created_at"],
            edited_at=row["edited_at"],
            parent_id=row["parent_id"],
            reactions=_parse_reactions(row["reactions_raw"]),
            replies=[],
        )
        by_id[c.id] = c
        if c.parent_id is None:
            top.append(c)
    for c in by_id.values():
        if c.parent_id and c.parent_id in by_id:
            by_id[c.parent_id].replies.append(c)
    return top


async def _comment_row_for_actor(
    ctx: CallerContext,
    db: aiosqlite.Connection,
    comment_id: int,
):
    db.row_factory = aiosqlite.Row
    row = await (
        await db.execute(
            "SELECT * FROM comments WHERE id = ? AND deleted_at IS NULL",
            (comment_id,),
        )
    ).fetchone()
    if row is None:
        raise NotFoundError(code="comment_not_found", message="Comment not found")
    await _authorize(db, ctx, row["target_type"], row["target_id"])
    return row


async def _comment_response(
    db: aiosqlite.Connection, comment_id: int
) -> CommentResponse:
    db.row_factory = aiosqlite.Row
    row = await (
        await db.execute(
            "SELECT c.*, "
            "GROUP_CONCAT(cr.reaction || ':' || cr.created_by, '|') AS reactions_raw "
            "FROM comments c LEFT JOIN comment_reactions cr ON cr.comment_id = c.id "
            "WHERE c.id = ? GROUP BY c.id",
            (comment_id,),
        )
    ).fetchone()
    if row is None:
        raise NotFoundError(code="comment_not_found", message="Comment not found")
    return CommentResponse(
        id=row["id"],
        target_type=row["target_type"],
        target_id=row["target_id"],
        body=row["body"],
        status=row["status"],
        created_by=row["created_by"],
        created_at=row["created_at"],
        edited_at=row["edited_at"],
        parent_id=row["parent_id"],
        reactions=_parse_reactions(row["reactions_raw"]),
        replies=[],
    )


async def update_comment(
    ctx: CallerContext,
    db: aiosqlite.Connection,
    *,
    comment_id: int,
    body: str | None,
    status: str | None,
) -> CommentResponse:
    """Update an own comment only after resolving its workspace-owned target."""
    require_role_ctx(ctx, "viewer", "operator", "admin", "super_admin")
    row = await _comment_row_for_actor(ctx, db, comment_id)
    if row["created_by"] != ctx.username:
        raise AuthorizationError(
            code="not_comment_author", message="Not the comment author"
        )
    updates: dict[str, str] = {}
    if body is not None:
        red_body, _ = bug_reports_core.redact(
            bug_reports_core.cap(body, COMMENT_BODY_CAP)
        )
        if not red_body.strip():
            raise ValidationError(
                code="empty_comment",
                message="Comment body is empty after redaction.",
            )
        updates["body"] = red_body
    if status is not None:
        updates["status"] = status
    if not updates:
        raise ValidationError(code="no_updates", message="No fields to update")
    updates["edited_at"] = _now()
    set_clause = ", ".join(f"{key} = ?" for key in updates)
    await db.execute(
        f"UPDATE comments SET {set_clause} WHERE id = ?",  # noqa: S608 - fixed keys
        [*updates.values(), comment_id],
    )
    await db.commit()
    return await _comment_response(db, comment_id)


async def delete_comment(
    ctx: CallerContext, db: aiosqlite.Connection, *, comment_id: int
) -> None:
    """Soft-delete an own comment after the same non-enumerating target gate."""
    require_role_ctx(ctx, "viewer", "operator", "admin", "super_admin")
    row = await _comment_row_for_actor(ctx, db, comment_id)
    if row["created_by"] != ctx.username:
        raise AuthorizationError(
            code="not_comment_author", message="Not the comment author"
        )
    active_replies = (
        await (
            await db.execute(
                "SELECT COUNT(*) FROM comments "
                "WHERE parent_id = ? AND deleted_at IS NULL",
                (comment_id,),
            )
        ).fetchone()
    )[0]
    now = _now()
    if active_replies:
        await db.execute(
            "UPDATE comments SET body = '[deleted]', deleted_at = ? WHERE id = ?",
            (now, comment_id),
        )
    else:
        await db.execute(
            "UPDATE comments SET deleted_at = ? WHERE id = ?", (now, comment_id)
        )
    await db.commit()


async def add_reaction(
    ctx: CallerContext,
    db: aiosqlite.Connection,
    *,
    comment_id: int,
    reaction: str,
) -> dict[str, bool]:
    require_role_ctx(ctx, "viewer", "operator", "admin", "super_admin")
    await _comment_row_for_actor(ctx, db, comment_id)
    try:
        await db.execute(
            "INSERT INTO comment_reactions "
            "(comment_id, reaction, created_by, created_at) VALUES (?, ?, ?, ?)",
            (comment_id, reaction, ctx.username, _now()),
        )
        await db.commit()
    except aiosqlite.IntegrityError as exc:
        raise ConflictError(
            code="reaction_exists", message="Reaction already exists"
        ) from exc
    return {"ok": True}


async def remove_reaction(
    ctx: CallerContext,
    db: aiosqlite.Connection,
    *,
    comment_id: int,
    reaction: str,
) -> dict[str, bool]:
    require_role_ctx(ctx, "viewer", "operator", "admin", "super_admin")
    await _comment_row_for_actor(ctx, db, comment_id)
    cursor = await db.execute(
        "DELETE FROM comment_reactions "
        "WHERE comment_id = ? AND reaction = ? AND created_by = ?",
        (comment_id, reaction, ctx.username),
    )
    await db.commit()
    if cursor.rowcount == 0:
        raise NotFoundError(code="reaction_not_found", message="Reaction not found")
    return {"ok": True}
