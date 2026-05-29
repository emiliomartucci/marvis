import asyncio

import pytest

from core.api.terminal import SessionManager, TerminalSession


@pytest.mark.asyncio
async def test_detach_cleans_up_orphan_pty_immediately(monkeypatch):
    manager = SessionManager()
    created_sessions: list[TerminalSession] = []
    cleaned_sessions: list[str] = []

    async def fake_create_pty(session_name: str, cols: int = 80, rows: int = 24):
        session = TerminalSession(
            name=session_name,
            master_fd=10 + len(created_sessions),
            pid=20 + len(created_sessions),
        )
        created_sessions.append(session)
        return session

    async def fake_cleanup(session: TerminalSession):
        cleaned_sessions.append(session.name)

    monkeypatch.setattr(manager, "_create_pty", fake_create_pty)
    monkeypatch.setattr(manager, "_cleanup_session", fake_cleanup)

    ws1 = object()
    ws2 = object()

    first = await manager.attach("test2", ws1, 120, 40)
    assert first.connections == {ws1}
    assert "test2" in manager._sessions

    await manager.detach("test2", ws1)
    assert cleaned_sessions == ["test2"]
    assert "test2" not in manager._sessions

    second = await manager.attach("test2", ws2, 120, 40)
    assert second.connections == {ws2}
    assert created_sessions == [first, second]


@pytest.mark.asyncio
async def test_detach_keeps_session_alive_when_another_client_is_connected(monkeypatch):
    manager = SessionManager()
    session = TerminalSession(name="test2", master_fd=11, pid=22)
    session.connections = set()
    manager._sessions["test2"] = session
    cleaned_sessions: list[str] = []

    async def fake_cleanup(session_to_cleanup: TerminalSession):
        cleaned_sessions.append(session_to_cleanup.name)

    monkeypatch.setattr(manager, "_cleanup_session", fake_cleanup)

    ws1 = object()
    ws2 = object()
    session.connections.update({ws1, ws2})

    await manager.detach("test2", ws1)

    assert cleaned_sessions == []
    assert manager._sessions["test2"] is session
    assert session.connections == {ws2}


@pytest.mark.asyncio
async def test_attach_recreates_stale_pty_after_reader_exits(monkeypatch):
    manager = SessionManager()
    stale = TerminalSession(name="test3", master_fd=12, pid=23)
    stale.connections = {object()}
    stale._reader_task = asyncio.create_task(asyncio.sleep(0))
    await stale._reader_task
    manager._sessions["test3"] = stale

    cleaned_sessions: list[str] = []
    created_sessions: list[TerminalSession] = []

    async def fake_cleanup(session_to_cleanup: TerminalSession):
        cleaned_sessions.append(session_to_cleanup.name)

    async def fake_create_pty(session_name: str, cols: int = 80, rows: int = 24):
        session = TerminalSession(name=session_name, master_fd=99, pid=199)
        created_sessions.append(session)
        return session

    monkeypatch.setattr(manager, "_cleanup_session", fake_cleanup)
    monkeypatch.setattr(manager, "_create_pty", fake_create_pty)

    ws = object()
    recreated = await manager.attach("test3", ws, 120, 40)

    assert cleaned_sessions == ["test3"]
    assert created_sessions == [recreated]
    assert manager._sessions["test3"] is recreated
    assert recreated.connections == {ws}
