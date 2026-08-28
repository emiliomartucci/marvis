# v1.1.0 - 2026-07-03 - P1 F2: create/list route through the shared comments use_case (RBAC + redaction)
from __future__ import annotations

import aiosqlite
from fastapi import APIRouter, Depends, Query

from core.api.db import get_db, get_write_db
from core.api.models import (
    CommentCreateRequest,
    CommentResponse,
    CommentUpdateRequest,
    ReactionCreateRequest,
    UserInfo,
)
from core.api.routers._adapter import to_http
from core.api.security import get_current_user_or_agent
from core.api.use_cases import comments as comments_uc
from core.api.use_cases._context import CallerContext
from core.api.use_cases._errors import ServiceError

router = APIRouter(prefix="/api/v1/comments", tags=["comments"])


@router.post("", status_code=201)
async def create_comment(
    body: CommentCreateRequest,
    user: UserInfo = Depends(get_current_user_or_agent),
    db: aiosqlite.Connection = Depends(get_write_db),
) -> CommentResponse:
    """Create a new comment (RBAC + redaction + notify via the shared use_case)."""
    ctx = CallerContext.from_user_info(user, is_human_session=user.user_type == "human")
    try:
        return await comments_uc.create_comment(
            ctx,
            db,
            target_type=body.target_type,
            target_id=body.target_id,
            body=body.body,
            status=body.status,
            parent_id=body.parent_id,
        )
    except ServiceError as e:
        raise to_http(e)


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
    """List comments with reactions, RBAC-gated through the shared use_case."""
    ctx = CallerContext.from_user_info(user, is_human_session=user.user_type == "human")
    try:
        return await comments_uc.list_comments(
            ctx,
            db,
            target_type=target_type,
            target_id=target_id,
            status=status,
            created_by=created_by,
            limit=limit,
            offset=offset,
        )
    except ServiceError as e:
        raise to_http(e)


@router.patch("/{comment_id}")
async def update_comment(
    comment_id: int,
    body: CommentUpdateRequest,
    user: UserInfo = Depends(get_current_user_or_agent),
    db: aiosqlite.Connection = Depends(get_write_db),
) -> CommentResponse:
    """Edit comment body/status after workspace-owned target authorization."""
    ctx = CallerContext.from_user_info(user, is_human_session=user.user_type == "human")
    try:
        return await comments_uc.update_comment(
            ctx,
            db,
            comment_id=comment_id,
            body=body.body,
            status=body.status,
        )
    except ServiceError as e:
        raise to_http(e)


@router.delete("/{comment_id}", status_code=204)
async def delete_comment(
    comment_id: int,
    user: UserInfo = Depends(get_current_user_or_agent),
    db: aiosqlite.Connection = Depends(get_write_db),
) -> None:
    """Soft delete an own comment after workspace-owned target authorization."""
    ctx = CallerContext.from_user_info(user, is_human_session=user.user_type == "human")
    try:
        await comments_uc.delete_comment(ctx, db, comment_id=comment_id)
    except ServiceError as e:
        raise to_http(e)


@router.post("/{comment_id}/reactions", status_code=201)
async def add_reaction(
    comment_id: int,
    body: ReactionCreateRequest,
    user: UserInfo = Depends(get_current_user_or_agent),
    db: aiosqlite.Connection = Depends(get_write_db),
) -> dict:
    """Add a reaction after workspace-owned target authorization."""
    ctx = CallerContext.from_user_info(user, is_human_session=user.user_type == "human")
    try:
        return await comments_uc.add_reaction(
            ctx,
            db,
            comment_id=comment_id,
            reaction=body.reaction,
        )
    except ServiceError as e:
        raise to_http(e)


@router.delete("/{comment_id}/reactions/{reaction}")
async def remove_reaction(
    comment_id: int,
    reaction: str,
    user: UserInfo = Depends(get_current_user_or_agent),
    db: aiosqlite.Connection = Depends(get_write_db),
) -> dict:
    """Remove an own reaction after workspace-owned target authorization."""
    ctx = CallerContext.from_user_info(user, is_human_session=user.user_type == "human")
    try:
        return await comments_uc.remove_reaction(
            ctx,
            db,
            comment_id=comment_id,
            reaction=reaction,
        )
    except ServiceError as e:
        raise to_http(e)
