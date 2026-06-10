# v1.0.0 - 2026-02-25 - Comments router: polymorphic comments with reactions
from __future__ import annotations

import logging
import secrets
from datetime import datetime, timezone

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException, Query

from core.api.db import get_db, get_write_db
from core.api.models import (
    CommentCreateRequest,
    CommentReaction,
    CommentResponse,
    CommentUpdateRequest,
    ReactionCreateRequest,
    UserInfo,
)
from core.api.security import get_current_user_or_agent

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/comments", tags=["comments"])


def _parse_reactions(reactions_raw: str | None) -> list[CommentReaction]:
    """Parse GROUP_CONCAT reactions string into list."""
    if not reactions_raw:
        return []
    result: list[CommentReaction] = []
    for part in reactions_raw.split("|"):
        pieces = part.split(":", 1)
        if len(pieces) == 2:
            result.append(CommentReaction(reaction=pieces[0], created_by=pieces[1]))
    return result


@router.post("", status_code=201)
async def create_comment(
    body: CommentCreateRequest,
    user: UserInfo = Depends(get_current_user_or_agent),
    db: aiosqlite.Connection = Depends(get_write_db),
) -> CommentResponse:
    """Create a new comment."""
    now = datetime.now(timezone.utc).isoformat()
    try:
        cursor = await db.execute(
            "INSERT INTO comments (target_type, target_id, body, status, created_by, created_at, parent_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (body.target_type, body.target_id, body.body, body.status, user.username, now, body.parent_id),
        )
        await db.commit()
    except aiosqlite.IntegrityError as e:
        error_msg = str(e)
        if "max depth" in error_msg.lower():
            raise HTTPException(422, "Cannot reply to a reply (max depth 1)")
        raise HTTPException(422, f"Integrity error: {error_msg}")

    comment_id = cursor.lastrowid
    return CommentResponse(
        id=comment_id,
        target_type=body.target_type,
        target_id=body.target_id,
        body=body.body,
        status=body.status,
        created_by=user.username,
        created_at=now,
        edited_at=None,
        parent_id=body.parent_id,
        reactions=[],
        replies=[],
    )


@router.get("")
async def list_comments(
    target_type: str = Query(...),
    target_id: str = Query(...),
    status: str | None = None,
    created_by: str | None = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user: UserInfo = Depends(get_current_user_or_agent),
    db: aiosqlite.Connection = Depends(get_db),
) -> list[CommentResponse]:
    """List comments with reactions (single JOIN query)."""
    query = """
        SELECT c.*, GROUP_CONCAT(
            cr.reaction || ':' || cr.created_by, '|'
        ) as reactions_raw
        FROM comments c
        LEFT JOIN comment_reactions cr ON cr.comment_id = c.id
        WHERE c.target_type = ? AND c.target_id = ? AND c.deleted_at IS NULL
    """
    params: list = [target_type, target_id]
    if status:
        query += " AND c.status = ?"
        params.append(status)
    if created_by:
        query += " AND c.created_by = ?"
        params.append(created_by)
    query += " GROUP BY c.id ORDER BY c.created_at ASC LIMIT ? OFFSET ?"
    params.extend([limit, offset])

    cursor = await db.execute(query, params)
    rows = await cursor.fetchall()

    # Assemble thread structure in Python (O(n))
    comments_by_id: dict[int, CommentResponse] = {}
    top_level: list[CommentResponse] = []
    for row in rows:
        reactions = _parse_reactions(row["reactions_raw"])
        comment = CommentResponse(
            id=row["id"],
            target_type=row["target_type"],
            target_id=row["target_id"],
            body=row["body"],
            status=row["status"],
            created_by=row["created_by"],
            created_at=row["created_at"],
            edited_at=row["edited_at"],
            parent_id=row["parent_id"],
            reactions=reactions,
            replies=[],
        )
        comments_by_id[comment.id] = comment
        if comment.parent_id is None:
            top_level.append(comment)

    # Attach replies to parents
    for comment in comments_by_id.values():
        if comment.parent_id and comment.parent_id in comments_by_id:
            comments_by_id[comment.parent_id].replies.append(comment)

    return top_level


@router.patch("/{comment_id}")
async def update_comment(
    comment_id: int,
    body: CommentUpdateRequest,
    user: UserInfo = Depends(get_current_user_or_agent),
    db: aiosqlite.Connection = Depends(get_write_db),
) -> CommentResponse:
    """Edit comment body/status (author only)."""
    cursor = await db.execute(
        "SELECT * FROM comments WHERE id = ? AND deleted_at IS NULL", (comment_id,)
    )
    row = await cursor.fetchone()
    if not row:
        raise HTTPException(404, "Comment not found")
    if not secrets.compare_digest(row["created_by"], user.username):
        raise HTTPException(403, "Not the comment author")

    updates: dict[str, str] = {}
    if body.body is not None:
        updates["body"] = body.body
    if body.status is not None:
        updates["status"] = body.status
    if not updates:
        raise HTTPException(422, "No fields to update")

    now = datetime.now(timezone.utc).isoformat()
    updates["edited_at"] = now
    set_clause = ", ".join(f"{k} = ?" for k in updates)
    values = list(updates.values()) + [comment_id]
    await db.execute(f"UPDATE comments SET {set_clause} WHERE id = ?", values)
    await db.commit()

    # Re-fetch with reactions
    cursor = await db.execute(
        "SELECT c.*, GROUP_CONCAT(cr.reaction || ':' || cr.created_by, '|') as reactions_raw "
        "FROM comments c LEFT JOIN comment_reactions cr ON cr.comment_id = c.id "
        "WHERE c.id = ? GROUP BY c.id",
        (comment_id,),
    )
    row = await cursor.fetchone()
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


@router.delete("/{comment_id}", status_code=204)
async def delete_comment(
    comment_id: int,
    user: UserInfo = Depends(get_current_user_or_agent),
    db: aiosqlite.Connection = Depends(get_write_db),
) -> None:
    """Soft delete comment (author only). Reddit-style if has replies."""
    cursor = await db.execute(
        "SELECT * FROM comments WHERE id = ? AND deleted_at IS NULL", (comment_id,)
    )
    row = await cursor.fetchone()
    if not row:
        raise HTTPException(404, "Comment not found")
    if not secrets.compare_digest(row["created_by"], user.username):
        raise HTTPException(403, "Not the comment author")

    # Check for active replies
    cursor = await db.execute(
        "SELECT COUNT(*) FROM comments WHERE parent_id = ? AND deleted_at IS NULL",
        (comment_id,),
    )
    active_replies = (await cursor.fetchone())[0]

    now = datetime.now(timezone.utc).isoformat()
    if active_replies > 0:
        # Reddit-style: replace body, keep structure
        await db.execute(
            "UPDATE comments SET body = '[deleted]', deleted_at = ? WHERE id = ?",
            (now, comment_id),
        )
    else:
        await db.execute(
            "UPDATE comments SET deleted_at = ? WHERE id = ?",
            (now, comment_id),
        )
    await db.commit()


@router.post("/{comment_id}/reactions", status_code=201)
async def add_reaction(
    comment_id: int,
    body: ReactionCreateRequest,
    user: UserInfo = Depends(get_current_user_or_agent),
    db: aiosqlite.Connection = Depends(get_write_db),
) -> dict:
    """Add reaction to comment."""
    # Verify comment exists
    cursor = await db.execute(
        "SELECT 1 FROM comments WHERE id = ? AND deleted_at IS NULL", (comment_id,)
    )
    if not await cursor.fetchone():
        raise HTTPException(404, "Comment not found")

    try:
        await db.execute(
            "INSERT INTO comment_reactions (comment_id, reaction, created_by, created_at) VALUES (?, ?, ?, ?)",
            (comment_id, body.reaction, user.username, datetime.now(timezone.utc).isoformat()),
        )
        await db.commit()
    except aiosqlite.IntegrityError:
        raise HTTPException(409, "Reaction already exists")
    return {"ok": True}


@router.delete("/{comment_id}/reactions/{reaction}")
async def remove_reaction(
    comment_id: int,
    reaction: str,
    user: UserInfo = Depends(get_current_user_or_agent),
    db: aiosqlite.Connection = Depends(get_write_db),
) -> dict:
    """Remove own reaction from comment."""
    cursor = await db.execute(
        "DELETE FROM comment_reactions WHERE comment_id = ? AND reaction = ? AND created_by = ?",
        (comment_id, reaction, user.username),
    )
    await db.commit()
    if cursor.rowcount == 0:
        raise HTTPException(404, "Reaction not found")
    return {"ok": True}
