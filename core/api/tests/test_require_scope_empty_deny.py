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
from core.api.use_cases._context import CallerContext


def _agent(scopes: list[str]) -> UserInfo:
    return UserInfo(
        username="tester",
        user_id="tok-1",
        system_role="operator",
        user_type="agent",
        scopes=scopes,
    ).with_auth_mechanism("agent_token")


def _human(scopes: list[str]) -> UserInfo:
    return UserInfo(
        username="emilio",
        user_id="user-1",
        system_role="operator",
        user_type="human",
        scopes=scopes,
    ).with_auth_mechanism("session")


def _bearer_human(scopes: list[str]) -> UserInfo:
    return UserInfo(
        username="ordinary-human-name",
        user_id="user-2",
        system_role="operator",
        user_type="human",
        scopes=scopes,
    ).with_auth_mechanism("agent_token")


def _local_human(scopes: list[str]) -> UserInfo:
    return UserInfo(
        username="local",
        user_id="local",
        system_role="operator",
        user_type="human",
        scopes=scopes,
    ).with_auth_mechanism("local")


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
    assert user.username == "tester"


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


@pytest.mark.asyncio
async def test_bearer_owned_human_identity_does_not_bypass_scope_check():
    check = require_scope("write")
    with pytest.raises(HTTPException) as exc:
        await check(user=_bearer_human([]))
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_local_single_user_bypasses_scope_check_as_validated_human():
    check = require_scope("write")
    user = await check(user=_local_human([]))
    assert user.username == "local"


@pytest.mark.asyncio
async def test_unknown_auth_mechanism_fails_closed_for_scoped_access():
    check = require_scope("write")
    with pytest.raises(HTTPException) as exc:
        await check(
            user=UserInfo(
                username="unbound",
                user_id="user-unbound",
                system_role="operator",
                user_type="human",
                scopes=["write"],
            )
        )
    assert exc.value.status_code == 403


def test_auth_mechanism_is_internal_and_does_not_widen_userinfo_contract():
    user = _human([])
    assert "auth_mechanism" not in user.model_dump()
    assert "auth_mechanism" not in UserInfo.model_json_schema()["properties"]


def test_local_user_is_trusted_human():
    ctx = CallerContext.from_user_info(_local_human([]), is_human_session=True)

    assert ctx.is_local_os_account is True
    assert ctx.user_type == "human"
    assert ctx.can_act_as_approver is True


def test_local_mcp_keeps_agent_context():
    from core.api.mcp._adapter import _build_local_ctx_from_env

    ctx = _build_local_ctx_from_env({"MARVIS_MCP_LOCAL_USER_TYPE": "human"})

    assert ctx.is_local_os_account is True
    assert ctx.user_type == "agent"
    assert ctx.is_human_session is False
    assert ctx.can_act_as_approver is False


def test_authenticated_human_is_allowed():
    ctx = CallerContext.from_user_info(_human([]), is_human_session=True)

    assert ctx.is_local_os_account is False
    assert ctx.can_act_as_approver is True


def test_server_agent_cannot_promote_to_human():
    ctx = CallerContext.from_user_info(_agent(["write"]), is_human_session=False)

    assert ctx.user_type == "agent"
    assert ctx.can_act_as_approver is False


def test_server_rejects_local_ctx_reuse():
    spoof = UserInfo(
        username="local",
        user_id="local",
        system_role="operator",
        user_type="agent",
        workspace_id="ws_default",
    ).with_auth_mechanism("agent_token")

    ctx = CallerContext.from_user_info(spoof, is_human_session=False)

    assert ctx.local_runtime is False
    assert ctx.is_local_os_account is False
    assert ctx.can_act_as_approver is False


def test_legacy_human_session_flag_is_ignored():
    ctx = CallerContext.from_user_info(_agent(["write"]), is_human_session=True)

    assert ctx.user_type == "agent"
    assert ctx.is_human_session is True
    assert ctx.can_act_as_approver is False
