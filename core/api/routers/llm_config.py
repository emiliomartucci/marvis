# v1.0.0 - 2026-05-26 - M1 CAPTURE U5 — per-function LLM config + provider keys (admin)
from __future__ import annotations

import logging

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException, Path as PathParam

from core.api.db import get_db, get_write_db
from core.api.models import UserInfo
from core.api.models.llm_config import (
    LLMFunction,
    LLMFunctionConfigItem,
    LLMFunctionConfigUpdate,
    ProviderKeyCreateRequest,
    ProviderKeyResponse,
)
from core.api.rbac import ROLE_HIERARCHY
from core.api.security import get_current_user_or_delegated_agent, is_local_single_user_mode
from core.api.services.ingest.llm import config_store

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/llm-config", tags=["llm-config"])

_FUNCTIONS = ("classify", "embedding", "brain")


async def require_llm_config_access(
    caller: UserInfo = Depends(get_current_user_or_delegated_agent),
) -> UserInfo:
    """Access control for LLM/BYOK config (gh #22 round 2).

    Managed/multi-user tier: admin+ humans only — parity with the original
    ``require_role('admin','super_admin', human_only=True)`` (the auth dependency
    already enforces human-or-delegated-grant). Local single-user OSS tier: the
    loopback owner manages their OWN provider keys (there is no admin above them);
    in that mode the auth dependency resolves to the local operator, so accept it.
    The managed tier is NOT weakened.
    """
    if is_local_single_user_mode():
        return caller
    if ROLE_HIERARCHY.get(caller.system_role, -1) < ROLE_HIERARCHY["admin"]:
        raise HTTPException(status_code=403, detail="Insufficient permissions")
    return caller


def _ws(caller) -> str:
    return getattr(caller, "workspace_id", None) or "ws_default"


@router.get("/status")
async def get_llm_status(
    caller=Depends(require_llm_config_access),
    db: aiosqlite.Connection = Depends(get_db),
) -> dict[str, object]:
    """Auto-classify provider status + encryption readiness for the Console.

    ``classify`` drives the 'configure a provider' alert ('configured' or
    'disabled_no_provider'). ``encryption_configured`` lets the Console surface
    the missing-``BYOK_FERNET_SECRET`` state proactively (provider-key creation
    is fail-closed → 503), instead of only on a failed save (gh #22 QA gap)."""
    from core.api.services.crypto import org_encryption_available

    return {
        "classify": await config_store.classify_provider_status(db, _ws(caller)),
        "encryption_configured": org_encryption_available(),
    }


@router.get("", response_model=list[LLMFunctionConfigItem])
async def get_llm_config(
    caller=Depends(require_llm_config_access),
    db: aiosqlite.Connection = Depends(get_db),
) -> list[LLMFunctionConfigItem]:
    rows = await config_store.list_function_configs(db, _ws(caller))
    return [LLMFunctionConfigItem(**row) for row in rows]


@router.put("/{function_name}", response_model=LLMFunctionConfigItem)
async def put_llm_config(
    body: LLMFunctionConfigUpdate,
    function_name: LLMFunction = PathParam(...),
    caller=Depends(require_llm_config_access),
    db: aiosqlite.Connection = Depends(get_write_db),
) -> LLMFunctionConfigItem:
    if function_name not in _FUNCTIONS:
        raise HTTPException(status_code=422, detail=f"unknown function {function_name!r}")
    ws = _ws(caller)
    if body.provider_key_id is not None:
        async with db.execute(
            "SELECT 1 FROM provider_keys WHERE id = ? AND workspace_id = ?",
            (body.provider_key_id, ws),
        ) as cur:
            if await cur.fetchone() is None:
                raise HTTPException(status_code=404, detail="provider_key_id not found")
    await config_store.set_function_config(
        db,
        function_name=function_name,
        provider_key_id=body.provider_key_id,
        model=body.model,
        enabled=body.enabled,
        workspace_id=ws,
    )
    await db.commit()
    rows = await config_store.list_function_configs(db, ws)
    item = next(r for r in rows if r["function_name"] == function_name)
    return LLMFunctionConfigItem(**item)


@router.get("/provider-keys", response_model=list[ProviderKeyResponse])
async def list_provider_keys(
    caller=Depends(require_llm_config_access),
    db: aiosqlite.Connection = Depends(get_db),
) -> list[ProviderKeyResponse]:
    rows = await config_store.list_provider_keys(db, _ws(caller))
    return [ProviderKeyResponse(**row) for row in rows]


@router.post("/provider-keys", response_model=ProviderKeyResponse, status_code=201)
async def create_provider_key(
    body: ProviderKeyCreateRequest,
    caller=Depends(require_llm_config_access),
    db: aiosqlite.Connection = Depends(get_write_db),
) -> ProviderKeyResponse:
    ws = _ws(caller)
    try:
        key_id = await config_store.create_provider_key(
            db,
            provider=body.provider,
            label=body.label,
            api_key=body.api_key,
            base_url=body.base_url,
            workspace_id=ws,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except RuntimeError:
        # crypto fail-closed (BYOK_FERNET_SECRET missing) — never echo internals.
        logger.error("create_provider_key: encryption unavailable", exc_info=True)
        raise HTTPException(
            status_code=503,
            detail="Provider key storage unavailable: server encryption is not configured.",
        )
    await db.commit()
    rows = await config_store.list_provider_keys(db, ws)
    row = next(r for r in rows if r["id"] == key_id)
    return ProviderKeyResponse(**row)


@router.delete("/provider-keys/{key_id}", status_code=204)
async def delete_provider_key(
    key_id: str,
    caller=Depends(require_llm_config_access),
    db: aiosqlite.Connection = Depends(get_write_db),
) -> None:
    ws = _ws(caller)
    deleted = await config_store.delete_provider_key(db, key_id, ws)
    await db.commit()
    if not deleted:
        raise HTTPException(status_code=404, detail="provider key not found")
