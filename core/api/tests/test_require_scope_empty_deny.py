"""Regression: require_scope must DENY agent tokens with empty/null scopes.

Audit S4: an agent token with scopes=[] previously satisfied `not user.scopes`
and was treated as unrestricted (authz bypass — a scope-gated PATCH succeeded
with an empty-scope token). Empty scopes must grant NO permission.
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from core.api.models.auth import UserInfo
from core.api.rbac import require_scope


def _agent(scopes: list[str]) -> UserInfo:
    # "agent:" prefix forces the agent path (not the human cookie bypass).
    return UserInfo(
        username="agent:tester",
        user_id="tok-1",
        system_role="operator",
        user_type="agent",
        scopes=scopes,
    )


def _human(scopes: list[str]) -> UserInfo:
    return UserInfo(
        username="emilio",
        user_id="user-1",
        system_role="operator",
        user_type="human",
        scopes=scopes,
    )


@pytest.mark.asyncio
async def test_empty_scopes_agent_denied():
    check = require_scope("write")
    with pytest.raises(HTTPException) as exc:
        await check(user=_agent([]))
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_matching_scope_agent_allowed():
    check = require_scope("write")
    user = await check(user=_agent(["read", "write"]))
    assert user.username == "agent:tester"


@pytest.mark.asyncio
async def test_missing_scope_agent_denied():
    check = require_scope("write")
    with pytest.raises(HTTPException) as exc:
        await check(user=_agent(["read"]))
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_human_bypasses_scope_check_even_with_empty_scopes():
    # Cookie/JWT humans are gated by role, not scopes — empty scopes is fine.
    check = require_scope("write")
    user = await check(user=_human([]))
    assert user.username == "emilio"
