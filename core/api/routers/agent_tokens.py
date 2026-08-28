"""Thin HTTP adapter for the per-principal agent-token lifecycle."""
from __future__ import annotations

import aiosqlite
from fastapi import APIRouter, Depends, Request

from core.api.db import get_db, get_write_db
from core.api.models import AgentTokenCreateRequest, AgentTokenResponse, UserInfo
from core.api.rbac import require_role
from core.api.routers._adapter import to_http
from core.api.security import require_any_auth
from core.api.use_cases import agent_tokens as uc
from core.api.use_cases._context import CallerContext
from core.api.use_cases._errors import ServiceError


router = APIRouter(prefix="/api/v1/agent-tokens", tags=["agent-tokens"])

# Backwards-compatible import seam used by ingestion tests and integrations.
_hash_token = uc.hash_token


def _ctx(user: UserInfo) -> CallerContext:
    return CallerContext.from_user_info(
        user,
        is_human_session=user.auth_mechanism in {"session", "local"},
    )


@router.post("", response_model=AgentTokenResponse, status_code=201)
async def create_agent_token(
    body: AgentTokenCreateRequest,
    caller: UserInfo = Depends(require_role("admin", "super_admin", human_only=True)),
    db: aiosqlite.Connection = Depends(get_write_db),
) -> AgentTokenResponse:
    try:
        return await uc.create_token(
            _ctx(caller), db, body=body, personal=False
        )
    except ServiceError as exc:
        raise to_http(exc) from exc


@router.get("", response_model=list[AgentTokenResponse])
async def list_agent_tokens(
    caller: UserInfo = Depends(require_role("admin", "super_admin", human_only=True)),
    db: aiosqlite.Connection = Depends(get_db),
) -> list[AgentTokenResponse]:
    try:
        return await uc.list_tokens(
            _ctx(caller), db, personal=False
        )
    except ServiceError as exc:
        raise to_http(exc) from exc


@router.delete("/{token_id}", status_code=204)
async def revoke_agent_token(
    token_id: str,
    caller: UserInfo = Depends(require_role("admin", "super_admin", human_only=True)),
    db: aiosqlite.Connection = Depends(get_write_db),
) -> None:
    try:
        await uc.revoke_token(
            _ctx(caller),
            db,
            token_id=token_id,
            personal=False,
        )
    except ServiceError as exc:
        raise to_http(exc) from exc


@router.post("/personal", response_model=AgentTokenResponse, status_code=201)
async def create_personal_token(
    body: AgentTokenCreateRequest,
    user: UserInfo = Depends(require_any_auth),
    db: aiosqlite.Connection = Depends(get_write_db),
) -> AgentTokenResponse:
    try:
        return await uc.create_token(
            _ctx(user),
            db,
            body=body,
            personal=True,
        )
    except ServiceError as exc:
        raise to_http(exc) from exc


@router.get("/personal", response_model=list[AgentTokenResponse])
async def list_personal_tokens(
    user: UserInfo = Depends(require_any_auth),
    db: aiosqlite.Connection = Depends(get_db),
) -> list[AgentTokenResponse]:
    try:
        return await uc.list_tokens(
            _ctx(user),
            db,
            personal=True,
        )
    except ServiceError as exc:
        raise to_http(exc) from exc


@router.delete("/personal/{token_id}", status_code=204)
async def revoke_personal_token(
    token_id: str,
    user: UserInfo = Depends(require_any_auth),
    db: aiosqlite.Connection = Depends(get_write_db),
) -> None:
    try:
        await uc.revoke_token(
            _ctx(user),
            db,
            token_id=token_id,
            personal=True,
        )
    except ServiceError as exc:
        raise to_http(exc) from exc


@router.post(
    "/personal/{token_id}/acknowledge",
    response_model=AgentTokenResponse,
)
async def acknowledge_personal_token_rotation(
    token_id: str,
    request: Request,
    user: UserInfo = Depends(require_any_auth),
    db: aiosqlite.Connection = Depends(get_write_db),
) -> AgentTokenResponse:
    try:
        return await uc.acknowledge_rotation(
            _ctx(user),
            db,
            token_id=token_id,
            authenticated_token_id=getattr(request.state, "agent_token_id", None),
        )
    except ServiceError as exc:
        raise to_http(exc) from exc
