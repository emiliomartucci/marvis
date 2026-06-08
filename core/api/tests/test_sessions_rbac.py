from __future__ import annotations

from types import SimpleNamespace

import pytest

from core.api.models.auth import UserInfo
from core.api.models.sessions import SessionInfo
from core.api.routers import sessions as sessions_router


def _request() -> SimpleNamespace:
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))


@pytest.mark.asyncio
async def test_marvisx_agent_keeps_global_visibility(monkeypatch: pytest.MonkeyPatch):
    sessions = [
        SessionInfo(name="alpha", owner_id="owner-1", provider="opencode"),
        SessionInfo(name="beta", owner_id="owner-2", provider="opencode"),
    ]

    async def _sync_sessions(_db):
        return sessions

    monkeypatch.setattr(sessions_router, "_sync_sessions", _sync_sessions)
    monkeypatch.setattr(sessions_router, "_sync_sessions_read_only", _sync_sessions)
    monkeypatch.setattr(sessions_router, "_sessions_cache", None)
    monkeypatch.setattr(sessions_router, "_sessions_cache_ts", 0.0)

    agent_user = UserInfo(
        username="marvisx",
        user_id="owner-1",
        system_role="operator",
        user_type="agent",
    )

    payload = await sessions_router.list_sessions(
        request=_request(), current_user=agent_user, db=SimpleNamespace()
    )

    assert [session.name for session in payload] == ["alpha", "beta"]


@pytest.mark.asyncio
async def test_non_system_agent_stays_owner_scoped(monkeypatch: pytest.MonkeyPatch):
    sessions = [
        SessionInfo(name="alpha", owner_id="owner-1", provider="opencode"),
        SessionInfo(name="beta", owner_id="owner-2", provider="opencode"),
    ]

    async def _sync_sessions(_db):
        return sessions

    monkeypatch.setattr(sessions_router, "_sync_sessions", _sync_sessions)
    monkeypatch.setattr(sessions_router, "_sync_sessions_read_only", _sync_sessions)
    monkeypatch.setattr(sessions_router, "_sessions_cache", None)
    monkeypatch.setattr(sessions_router, "_sessions_cache_ts", 0.0)

    agent_user = UserInfo(
        username="docs-bot",
        user_id="owner-1",
        system_role="operator",
        user_type="agent",
    )

    payload = await sessions_router.list_sessions(
        request=_request(), current_user=agent_user, db=SimpleNamespace()
    )

    assert [session.name for session in payload] == ["alpha"]


@pytest.mark.asyncio
async def test_prefixed_system_agent_keeps_global_visibility(
    monkeypatch: pytest.MonkeyPatch,
):
    sessions = [
        SessionInfo(name="alpha", owner_id="owner-1", provider="opencode"),
        SessionInfo(name="beta", owner_id="owner-2", provider="opencode"),
    ]

    async def _sync_sessions(_db):
        return sessions

    monkeypatch.setattr(sessions_router, "_sync_sessions", _sync_sessions)
    monkeypatch.setattr(sessions_router, "_sync_sessions_read_only", _sync_sessions)
    monkeypatch.setattr(sessions_router, "_sessions_cache", None)
    monkeypatch.setattr(sessions_router, "_sessions_cache_ts", 0.0)

    agent_user = UserInfo(
        username="agent:console-api",
        user_id="",
        system_role="viewer",
        user_type="agent",
    )

    payload = await sessions_router.list_sessions(
        request=_request(), current_user=agent_user, db=SimpleNamespace()
    )

    assert [session.name for session in payload] == ["alpha", "beta"]


@pytest.mark.asyncio
async def test_human_operator_keeps_owner_filter(monkeypatch: pytest.MonkeyPatch):
    sessions = [
        SessionInfo(name="alpha", owner_id="owner-1", provider="opencode"),
        SessionInfo(name="beta", owner_id="owner-2", provider="opencode"),
    ]

    async def _sync_sessions(_db):
        return sessions

    monkeypatch.setattr(sessions_router, "_sync_sessions", _sync_sessions)
    monkeypatch.setattr(sessions_router, "_sync_sessions_read_only", _sync_sessions)
    monkeypatch.setattr(sessions_router, "_sessions_cache", None)
    monkeypatch.setattr(sessions_router, "_sessions_cache_ts", 0.0)

    human_user = UserInfo(
        username="claudio",
        user_id="owner-1",
        system_role="operator",
        user_type="human",
    )

    payload = await sessions_router.list_sessions(
        request=_request(), current_user=human_user, db=SimpleNamespace()
    )

    assert [session.name for session in payload] == ["alpha"]
