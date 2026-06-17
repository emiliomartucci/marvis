# v1.0.0 - 2026-06-12 - Todos HTTP adapter
from __future__ import annotations

import aiosqlite
from fastapi import APIRouter, Body, Depends, Query

from core.api.db import get_db, get_write_db
from core.api.models.auth import UserInfo
from core.api.models.todos import (
    TodoCreateRequest,
    TodoDelegateRequest,
    TodoResponse,
    TodoUpdateRequest,
)
from core.api.routers._adapter import to_http
from core.api.routers.tasks import _schedule_embed_task
from core.api.security import get_current_user_or_agent
from core.api.services.graph_service import sync_task_to_graph
from core.api.use_cases import todos as uc
from core.api.use_cases._context import CallerContext
from core.api.use_cases._errors import ServiceError

router = APIRouter(prefix="/api/v1/todos", tags=["todos"])


@router.get("", response_model=list[TodoResponse])
async def list_todos(
    status: str | None = Query(None),
    type: str | None = Query(None),
    project: str | None = Query(None, pattern=r"^[a-z0-9][a-z0-9_.&\-]{0,126}$"),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    user: UserInfo = Depends(get_current_user_or_agent),
    db: aiosqlite.Connection = Depends(get_db),
) -> list[TodoResponse]:
    """List todos.

    Includes read-only virtual approva rows projected from:
    task_review (task in review with active PR), finding (pending/open Brain
    finding), memory_op (pending Brain memory operation). These virtual rows
    carry {"virtual": true, "origin": {"kind": "...", "id": "..."}} and must be
    acted on through the existing owning endpoints.
    """
    ctx = CallerContext.from_user_info(user, is_human_session=False)
    try:
        return await uc.list_todos(
            ctx,
            db,
            status=status,
            type=type,
            project=project,
            limit=limit,
            offset=offset,
        )
    except ServiceError as e:
        raise to_http(e)


@router.post("", status_code=201, response_model=TodoResponse)
async def create_todo(
    body: TodoCreateRequest,
    user: UserInfo = Depends(get_current_user_or_agent),
    db: aiosqlite.Connection = Depends(get_write_db),
) -> TodoResponse:
    ctx = CallerContext.from_user_info(user, is_human_session=False)
    try:
        return await uc.create_todo(ctx, db, body=body, created_by=user.username)
    except ServiceError as e:
        raise to_http(e)


@router.patch("/{todo_id}", response_model=TodoResponse)
async def update_todo(
    todo_id: str,
    body: TodoUpdateRequest,
    user: UserInfo = Depends(get_current_user_or_agent),
    db: aiosqlite.Connection = Depends(get_write_db),
) -> TodoResponse:
    ctx = CallerContext.from_user_info(user, is_human_session=False)
    try:
        return await uc.update_todo(
            ctx,
            db,
            todo_id=todo_id,
            body=body,
            sync_graph=sync_task_to_graph,
            schedule_embed=_schedule_embed_task,
        )
    except ServiceError as e:
        raise to_http(e)


@router.post("/{todo_id}/delegate", response_model=TodoResponse)
async def delegate_todo(
    todo_id: str,
    body: TodoDelegateRequest | None = Body(default=None),
    user: UserInfo = Depends(get_current_user_or_agent),
    db: aiosqlite.Connection = Depends(get_write_db),
) -> TodoResponse:
    ctx = CallerContext.from_user_info(user, is_human_session=False)
    try:
        return await uc.delegate_todo(
            ctx,
            db,
            todo_id=todo_id,
            body=body,
            sync_graph=sync_task_to_graph,
            schedule_embed=_schedule_embed_task,
        )
    except ServiceError as e:
        raise to_http(e)
