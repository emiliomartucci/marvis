from __future__ import annotations

import aiosqlite
from fastapi import APIRouter, Depends, Query

from core.api.db import get_write_db
from core.api.models.auth import UserInfo
from core.api.models.onboarding import (
    DemoSeedRequest,
    DemoSeedResponse,
    DemoTeardownResponse,
    ScanWorkdirRequest,
    ScanWorkdirResponse,
    SetupReadResponse,
    SetupWriteRequest,
)
from core.api.routers._adapter import to_http
from core.api.security import get_current_user_or_agent
from core.api.use_cases import onboarding as uc
from core.api.use_cases._context import CallerContext
from core.api.use_cases._errors import ServiceError

router = APIRouter(prefix="/api/v1/onboarding", tags=["onboarding"])


@router.post("/scan", response_model=ScanWorkdirResponse)
async def scan_workdir(
    body: ScanWorkdirRequest,
    _user: UserInfo = Depends(get_current_user_or_agent),
) -> ScanWorkdirResponse:
    try:
        return uc.scan_workdir(root=body.root, exclusions=body.exclusions)
    except ServiceError as e:
        raise to_http(e)


@router.get("/setup", response_model=SetupReadResponse)
async def read_setup(
    _user: UserInfo = Depends(get_current_user_or_agent),
) -> SetupReadResponse:
    return uc.read_setup()


@router.put("/setup", response_model=SetupReadResponse)
async def write_setup(
    body: SetupWriteRequest,
    _user: UserInfo = Depends(get_current_user_or_agent),
) -> SetupReadResponse:
    try:
        return uc.write_setup(
            section=body.section,
            content=body.content,
            checkboxes=body.checkboxes,
        )
    except ServiceError as e:
        raise to_http(e)


@router.post("/demo", response_model=DemoSeedResponse)
async def seed_demo(
    body: DemoSeedRequest | None = None,
    lang: str | None = Query(None, pattern="^(it|en)$"),
    user: UserInfo = Depends(get_current_user_or_agent),
    db: aiosqlite.Connection = Depends(get_write_db),
) -> DemoSeedResponse:
    ctx = CallerContext.from_user_info(user, is_human_session=False)
    selected_lang = lang or (body.lang if body else "it")
    try:
        return await uc.seed_demo(ctx, db, lang=selected_lang)
    except ServiceError as e:
        raise to_http(e)


@router.delete("/demo", response_model=DemoTeardownResponse)
async def teardown_demo(
    user: UserInfo = Depends(get_current_user_or_agent),
    db: aiosqlite.Connection = Depends(get_write_db),
) -> DemoTeardownResponse:
    ctx = CallerContext.from_user_info(user, is_human_session=False)
    try:
        return await uc.teardown_demo(ctx, db)
    except ServiceError as e:
        raise to_http(e)
