# v1.2.0 - 2026-04-14 - Single-writer: create_repo_share_link uses get_write_db (batch 6/6)
from __future__ import annotations

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException, Request

from core.api.db import get_db, get_write_db
from core.api.models import UserInfo
from core.api.security import (
    get_current_user_or_agent,
    is_local_single_user_mode,
    is_loopback_request,
)
from core.api.services.share_links import (
    create_shared_link_record,
    enforce_workspace_share_role,
    fetch_active_shared_path,
    mark_share_access,
    normalize_repo_input,
    public_repo_path,
    render_shared_target,
    resolve_shared_target,
    stored_repo_path,
    validate_repo_path,
)
from core.api.routers.finder import _validate_path

_LEGACY_SHARE_DETAIL = (
    "Legacy host-global share routes are local-only. Use the governed share_file "
    "MCP tool for remote project-owned sharing."
)


def _require_local_legacy_share_request(request: Request) -> None:
    if is_local_single_user_mode() and is_loopback_request(request):
        return
    raise HTTPException(status_code=403, detail=_LEGACY_SHARE_DETAIL)


router = APIRouter(
    prefix="/api/v1/share-repo",
    tags=["share-repo"],
    dependencies=[Depends(_require_local_legacy_share_request)],
)
shared_repo_router = APIRouter(
    prefix="/api/v1/shared-repo",
    tags=["share-repo"],
    dependencies=[Depends(_require_local_legacy_share_request)],
)


@router.post("")
async def create_repo_share_link(
    data: dict,
    current_user: UserInfo = Depends(get_current_user_or_agent),
    db: aiosqlite.Connection = Depends(get_write_db),
):
    """Legacy compatibility endpoint for workspace shares."""
    enforce_workspace_share_role(current_user)
    repo_rel_path = normalize_repo_input(data.get("path", ""))
    target = validate_repo_path(repo_rel_path)
    if not target.is_file():
        from fastapi import HTTPException
        raise HTTPException(404, "File not found")

    return await create_shared_link_record(
        stored_path=stored_repo_path(repo_rel_path),
        public_path=public_repo_path(repo_rel_path),
        current_user=current_user,
        db=db,
        hours=data.get("hours", 24),
    )


@shared_repo_router.get("/{token}")
async def access_shared_repo_file(
    token: str,
    request: Request,
    db: aiosqlite.Connection = Depends(get_db),
):
    """Legacy compatibility endpoint for previously issued workspace share URLs."""
    stored_path = await fetch_active_shared_path(token, db)
    target = resolve_shared_target(stored_path, _validate_path)
    await mark_share_access(token, db)
    return await render_shared_target(
        target,
        request,
        db,
        token=token,
        editable=True,
    )
