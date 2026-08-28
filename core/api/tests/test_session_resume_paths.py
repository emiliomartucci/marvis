from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.api.models.auth import UserInfo
from core.api.routers import agent as agent_router
from core.api.routers import sessions as sessions_router
from core.api.services import claude_metrics
from core.api.services.providers import PROVIDERS


class FakeAsyncCursor:
    def __init__(self, cursor: sqlite3.Cursor) -> None:
        self._cursor = cursor

    async def fetchone(self):
        return self._cursor.fetchone()

    async def fetchall(self):
        return self._cursor.fetchall()

    @property
    def rowcount(self) -> int:
        return self._cursor.rowcount


class FakeAsyncDB:
    def __init__(self, path: str) -> None:
        self._conn = sqlite3.connect(path)
        self._conn.row_factory = sqlite3.Row

    async def execute(self, query: str, params=()):
        cursor = self._conn.execute(query, params)
        return FakeAsyncCursor(cursor)

    async def commit(self) -> None:
        self._conn.commit()

    async def rollback(self) -> None:
        self._conn.rollback()

    def close(self) -> None:
        self._conn.close()


@pytest.fixture
def tmp_db(tmp_path: Path) -> str:
    db_path = str(tmp_path / "sessions.db")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE sessions_meta ("
        "name TEXT PRIMARY KEY, "
        "workspace_id TEXT NOT NULL DEFAULT 'ws_default', "
        "display_name TEXT, "
        "pinned INTEGER NOT NULL DEFAULT 0, "
        "sort_order INTEGER NOT NULL DEFAULT 0, "
        "group_name TEXT, "
        "session_uuid TEXT, "
        "created_at TEXT, "
        "last_active TEXT, "
        "hibernated INTEGER NOT NULL DEFAULT 0, "
        "hibernated_at TEXT, "
        "conversation_id TEXT, "
        "provider TEXT, "
        "project_slug TEXT, "
        "model TEXT, "
        "launch_model TEXT, "
        "permission_preset TEXT, "
        "theme_mode TEXT, "
        "auto_hibernate_minutes INTEGER, "
        "working_seconds INTEGER, "
        "agent_managed INTEGER NOT NULL DEFAULT 0, "
        "owner_id TEXT, "
        # Post-migration-088 schema: legacy column renamed
        "last_context_pct_legacy REAL, "
        # Post-migration-087 dual metrics columns (see migrations/087/088)
        "last_context_pct_real REAL, "
        "last_context_pct_scaled REAL, "
        "last_cost_usd REAL, "
        "last_cost_conversation_usd REAL, "
        "last_cost_session_usd REAL, "
        "last_cost_session_incomplete INTEGER, "
        "last_input_tokens INTEGER, "
        "last_output_tokens INTEGER, "
        "last_reasoning_tokens INTEGER, "
        "working_seconds_msg INTEGER, "
        "metrics_refreshed_at TEXT, "
        "pricing_version TEXT, "
        # Post-migration-089 shadow cost columns (PR4)
        "last_cost_conversation_equivalent_usd REAL, "
        "last_cost_session_equivalent_usd REAL, "
        "last_cost_equivalent_pricing_version TEXT, "
        "last_message_count INTEGER, "
        "last_metrics_at TEXT, "
        "activity_state TEXT, "
        "activity_state_updated_at TEXT, "
        "bootstrap_message TEXT)"
    )
    conn.execute(
        "CREATE TABLE session_operation_leases ("
        "workspace_id TEXT NOT NULL, "
        "session_name TEXT NOT NULL, "
        "session_uuid TEXT NOT NULL, "
        "generation INTEGER NOT NULL DEFAULT 0 CHECK (generation >= 0), "
        "operation TEXT, "
        "lease_expires_at TEXT, "
        "updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')), "
        "PRIMARY KEY (workspace_id, session_name))"
    )
    conn.execute(
        "CREATE TABLE workspace_projects ("
        "workspace_id TEXT NOT NULL, "
        "project_slug TEXT NOT NULL, "
        "source TEXT NOT NULL, "
        "created_by TEXT NOT NULL, "
        "PRIMARY KEY (workspace_id, project_slug))"
    )
    conn.execute(
        "INSERT INTO workspace_projects "
        "(workspace_id, project_slug, source, created_by) VALUES (?, ?, ?, ?)",
        ("ws_default", "c&i-normativa", "test", "test-fixture"),
    )
    conn.execute(
        "INSERT INTO sessions_meta (name, session_uuid, created_at, hibernated, conversation_id, provider, project_slug, launch_model, permission_preset, theme_mode, bootstrap_message) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "normativa",
            "41f2001f-d009-4ec0-87e2-ae86be23c53b",
            "2026-04-08T07:12:44+00:00",
            1,
            "41f2001f-d009-4ec0-87e2-ae86be23c53b",
            "claude",
            "c&i-normativa",
            "sonnet",
            None,
            None,
            "carica /data/projects/c&i-normativa",
        ),
    )
    conn.commit()
    conn.close()
    return db_path


def test_console_resume_uses_workspace_when_conversation_is_stored_there(
    tmp_db: str,
    monkeypatch: pytest.MonkeyPatch,
):
    db = FakeAsyncDB(tmp_db)
    sent: list[tuple[str, str, bool]] = []

    async def _session_exists(_name: str) -> bool:
        return True

    async def _send_keys(name: str, cmd: str, double_enter: bool = True) -> bool:
        sent.append((name, cmd, double_enter))
        return True

    async def _noop(*_args, **_kwargs) -> bool:
        return True

    async def _to_thread(func, /, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(sessions_router.tmux, "session_exists", _session_exists)
    monkeypatch.setattr(sessions_router.tmux, "send_keys", _send_keys)
    monkeypatch.setattr(sessions_router.tmux, "exit_copy_mode", _noop)
    monkeypatch.setattr(
        sessions_router.tmux, "validate_session_name", lambda name: name
    )
    monkeypatch.setattr(sessions_router.asyncio, "to_thread", _to_thread)
    monkeypatch.setattr(
        sessions_router,
        "resolve_project_path",
        lambda _slug: "/data/projects/c&i-normativa",
    )
    monkeypatch.setattr(
        sessions_router,
        "_resolve_conversation_cwd",
        lambda _conv_id, _slug: "/var/marvisx/workspace",
    )

    async def _run():
        return await sessions_router.resume_session(
            name="normativa",
            _user=UserInfo(username="emilio", system_role="admin"),
            db=db,
        )

    try:
        payload = asyncio.run(_run())
    finally:
        db.close()

    assert payload["status"] == "resumed"
    assert payload["conversation_id"] == "41f2001f-d009-4ec0-87e2-ae86be23c53b"
    assert len(sent) == 1
    assert sent[0][0] == "normativa"
    assert sent[0][2] is True
    assert (
        "cd /var/marvisx/workspace && claude --resume "
        "41f2001f-d009-4ec0-87e2-ae86be23c53b --dangerously-skip-permissions"
    ) in sent[0][1]


def test_console_resume_falls_back_to_continue_and_clears_stale_conversation(
    tmp_db: str,
    monkeypatch: pytest.MonkeyPatch,
):
    db = FakeAsyncDB(tmp_db)
    sent: list[str] = []

    async def _session_exists(_name: str) -> bool:
        return True

    async def _send_keys(_name: str, cmd: str, double_enter: bool = True) -> bool:
        sent.append(cmd)
        return True

    async def _noop(*_args, **_kwargs) -> bool:
        return True

    async def _to_thread(func, /, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(sessions_router.tmux, "session_exists", _session_exists)
    monkeypatch.setattr(sessions_router.tmux, "send_keys", _send_keys)
    monkeypatch.setattr(sessions_router.tmux, "exit_copy_mode", _noop)
    monkeypatch.setattr(
        sessions_router.tmux, "validate_session_name", lambda name: name
    )
    monkeypatch.setattr(sessions_router.asyncio, "to_thread", _to_thread)
    monkeypatch.setattr(
        sessions_router,
        "resolve_project_path",
        lambda _slug: "/data/projects/c&i-normativa",
    )
    monkeypatch.setattr(
        sessions_router, "_resolve_conversation_cwd", lambda _conv_id, _slug: None
    )

    async def _run():
        payload = await sessions_router.resume_session(
            name="normativa",
            _user=UserInfo(username="emilio", system_role="admin"),
            db=db,
        )
        cursor = await db.execute(
            "SELECT conversation_id, hibernated FROM sessions_meta WHERE name = ?",
            ("normativa",),
        )
        row = await cursor.fetchone()
        return payload, row

    try:
        payload, row = asyncio.run(_run())
    finally:
        db.close()

    assert payload["status"] == "resumed"
    assert payload["conversation_id"] is None
    assert len(sent) == 1
    assert (
        "cd '/data/projects/c&i-normativa' && claude --continue --dangerously-skip-permissions"
        in sent[0]
    )
    assert row["conversation_id"] is None
    assert row["hibernated"] == 0


def test_agent_resume_uses_project_aware_workspace_fallback(
    tmp_db: str,
    monkeypatch: pytest.MonkeyPatch,
):
    db = FakeAsyncDB(tmp_db)
    sent: list[tuple[str, str, bool]] = []

    async def _resolve_uuid(_uuid: str, _db, _workspace_id: str) -> str:
        return "normativa"

    async def _send_keys(name: str, cmd: str, double_enter: bool = True) -> bool:
        sent.append((name, cmd, double_enter))
        return True

    async def _to_thread(func, /, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(agent_router, "_resolve_uuid", _resolve_uuid)
    monkeypatch.setattr(agent_router.tmux, "send_keys", _send_keys)
    monkeypatch.setattr(agent_router.tmux, "validate_session_name", lambda name: name)
    monkeypatch.setattr(agent_router, "_log_action", lambda *args, **kwargs: None)
    monkeypatch.setattr(agent_router.asyncio, "to_thread", _to_thread)
    monkeypatch.setattr(
        agent_router,
        "resolve_project_path",
        lambda _slug: "/data/projects/c&i-normativa",
    )
    monkeypatch.setattr(
        claude_metrics,
        "find_conversation_cwd",
        lambda _conv_id, _paths: "/var/marvisx/workspace",
    )

    async def _run():
        return await agent_router.resume_session(
            uuid="41f2001f-d009-4ec0-87e2-ae86be23c53b",
            user=UserInfo(
                username="agent:rem", system_role="operator", user_type="agent"
            ),
            db=db,
        )

    try:
        payload = asyncio.run(_run())
    finally:
        db.close()

    assert payload["status"] == "resumed"
    assert payload["conversation_id"] == "41f2001f-d009-4ec0-87e2-ae86be23c53b"
    assert len(sent) == 1
    assert sent[0][2] is True
    assert (
        "cd /var/marvisx/workspace && claude --resume "
        "41f2001f-d009-4ec0-87e2-ae86be23c53b --dangerously-skip-permissions"
    ) in sent[0][1]


def test_codex_provider_uses_yolo_full_access():
    assert PROVIDERS["codex"].cli_flags == "--dangerously-bypass-approvals-and-sandbox"
    assert claude_metrics.normalize_cwd("~/workspace").endswith("/workspace")


def test_console_restart_recreates_non_claude_tmux_session(
    tmp_db: str,
    monkeypatch: pytest.MonkeyPatch,
):
    db = FakeAsyncDB(tmp_db)
    sent_raw: list[str] = []
    created: list[tuple[str, str | None]] = []
    killed: list[str] = []

    conn = sqlite3.connect(tmp_db)
    conn.execute(
        "INSERT INTO sessions_meta (name, session_uuid, hibernated, conversation_id, provider, project_slug, launch_model, permission_preset, theme_mode, bootstrap_message) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "opencode-test",
            "c6d113bc-8f6e-4d76-a8d5-b1b64e701111",
            0,
            None,
            "opencode",
            "marvisx",
            "openai/gpt-5.4",
            "yolo",
            "dark",
            None,
        ),
    )
    conn.commit()
    conn.close()

    async def _session_exists(_name: str) -> bool:
        return True

    async def _get_session_status(_name: str) -> str:
        return "opencode"

    async def _send_keys_raw(_name: str, *keys: str) -> bool:
        sent_raw.extend(keys)
        return True

    async def _kill_session(name: str) -> bool:
        killed.append(name)
        return True

    async def _create_session(
        name: str, start_dir: str = "", start_command: str | None = None
    ) -> bool:
        _ = start_dir
        created.append((name, start_command))
        return True

    async def _fail_send_keys(*_args, **_kwargs):
        raise AssertionError(
            "restart should recreate tmux session instead of injecting into existing pane"
        )

    async def _noop(*_args, **_kwargs):
        return True

    async def _sleep(_seconds: float):
        return None

    monkeypatch.setattr(sessions_router.tmux, "session_exists", _session_exists)
    monkeypatch.setattr(sessions_router.tmux, "get_session_status", _get_session_status)
    monkeypatch.setattr(sessions_router.tmux, "send_keys_raw", _send_keys_raw)
    monkeypatch.setattr(sessions_router.tmux, "kill_session", _kill_session)
    monkeypatch.setattr(sessions_router.tmux, "create_session", _create_session)
    monkeypatch.setattr(sessions_router.tmux, "send_keys", _fail_send_keys)
    monkeypatch.setattr(sessions_router.tmux, "exit_copy_mode", _noop)
    monkeypatch.setattr(
        sessions_router.tmux, "validate_session_name", lambda name: name
    )
    monkeypatch.setattr(
        sessions_router,
        "build_session_start_spec",
        lambda *_args, **_kwargs: SimpleNamespace(
            start_command="opencode start --fresh",
            launch_dir="/var/marvisx/workspace",
        ),
    )
    monkeypatch.setattr(sessions_router.asyncio, "sleep", _sleep)

    async def _run():
        return await sessions_router.restart_session(
            name="opencode-test",
            _user=UserInfo(username="emilio", system_role="admin"),
            db=db,
        )

    try:
        payload = asyncio.run(_run())
    finally:
        db.close()

    assert payload["status"] == "restarted"
    assert payload["resumed"] is False
    assert sent_raw == ["C-c", "/exit", "Enter"]
    assert killed == ["opencode-test"]
    assert created == [("opencode-test", "opencode start --fresh")]


def test_console_resume_reuses_stored_opencode_session_id(
    tmp_db: str,
    monkeypatch: pytest.MonkeyPatch,
):
    db = FakeAsyncDB(tmp_db)
    recreated: list[tuple[str, str]] = []

    conn = sqlite3.connect(tmp_db)
    conn.execute(
        "INSERT INTO sessions_meta (name, session_uuid, created_at, hibernated, conversation_id, provider, project_slug, launch_model, permission_preset, theme_mode, bootstrap_message) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "opencode-resume",
            "3cf7f6ef-0000-4d76-a8d5-b1b64e701111",
            "2026-04-08T07:12:44+00:00",
            1,
            "ses_296333d27ffeWpQ1ckV3VVikAy",
            "opencode",
            "marvisx",
            "openai/gpt-5.4",
            "yolo",
            "light",
            None,
        ),
    )
    conn.commit()
    conn.close()

    async def _session_exists(_name: str) -> bool:
        return True

    async def _noop(*_args, **_kwargs):
        return True

    async def _recreate(name: str, cmd: str) -> None:
        recreated.append((name, cmd))

    monkeypatch.setattr(sessions_router.tmux, "session_exists", _session_exists)
    monkeypatch.setattr(sessions_router.tmux, "exit_copy_mode", _noop)
    monkeypatch.setattr(
        sessions_router.tmux, "validate_session_name", lambda name: name
    )
    monkeypatch.setattr(sessions_router, "_recreate_tmux_session", _recreate)

    async def _run():
        return await sessions_router.resume_session(
            name="opencode-resume",
            _user=UserInfo(username="emilio", system_role="admin"),
            db=db,
        )

    try:
        payload = asyncio.run(_run())
    finally:
        db.close()

    assert payload["status"] == "resumed"
    assert payload["conversation_id"] == "ses_296333d27ffeWpQ1ckV3VVikAy"
    assert recreated[0][0] == "opencode-resume"
    assert "--session ses_296333d27ffeWpQ1ckV3VVikAy" in recreated[0][1]
    assert "--model openai/gpt-5.4" in recreated[0][1]
    assert '"permission":"allow"' in recreated[0][1]
    assert "tui.light.json" in recreated[0][1]


def test_console_restart_backfills_opencode_session_id_from_created_at(
    tmp_db: str,
    monkeypatch: pytest.MonkeyPatch,
):
    db = FakeAsyncDB(tmp_db)
    sent_raw: list[str] = []
    recreated: list[tuple[str, str]] = []

    conn = sqlite3.connect(tmp_db)
    conn.execute(
        "INSERT INTO sessions_meta (name, session_uuid, created_at, hibernated, conversation_id, provider, project_slug, launch_model, permission_preset, theme_mode, bootstrap_message) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "opencode-backfill",
            "4cf7f6ef-0000-4d76-a8d5-b1b64e701111",
            "2026-04-08T07:12:44+00:00",
            0,
            None,
            "opencode",
            "marvisx",
            "openai/gpt-5.4",
            "yolo",
            "dark",
            None,
        ),
    )
    conn.commit()
    conn.close()

    async def _session_exists(_name: str) -> bool:
        return True

    async def _get_session_status(_name: str) -> str:
        return "opencode"

    async def _send_keys_raw(_name: str, *keys: str) -> bool:
        sent_raw.extend(keys)
        return True

    async def _noop(*_args, **_kwargs):
        return True

    async def _sleep(_seconds: float):
        return None

    async def _recreate(name: str, cmd: str) -> None:
        recreated.append((name, cmd))

    async def _to_thread(func, /, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(sessions_router.tmux, "session_exists", _session_exists)
    monkeypatch.setattr(sessions_router.tmux, "get_session_status", _get_session_status)
    monkeypatch.setattr(sessions_router.tmux, "send_keys_raw", _send_keys_raw)
    monkeypatch.setattr(sessions_router.tmux, "exit_copy_mode", _noop)
    monkeypatch.setattr(
        sessions_router.tmux, "validate_session_name", lambda name: name
    )
    monkeypatch.setattr(sessions_router, "_recreate_tmux_session", _recreate)
    monkeypatch.setattr(sessions_router.asyncio, "sleep", _sleep)
    monkeypatch.setattr(sessions_router.asyncio, "to_thread", _to_thread)
    monkeypatch.setattr(
        sessions_router.opencode_sessions,
        "find_session_id_for_created_at",
        lambda *_args, **_kwargs: "ses_backfilled123",
    )

    async def _run():
        payload = await sessions_router.restart_session(
            name="opencode-backfill",
            _user=UserInfo(username="emilio", system_role="admin"),
            db=db,
        )
        cursor = await db.execute(
            "SELECT conversation_id FROM sessions_meta WHERE name = ?",
            ("opencode-backfill",),
        )
        row = await cursor.fetchone()
        return payload, row["conversation_id"]

    try:
        payload, stored_id = asyncio.run(_run())
    finally:
        db.close()

    assert payload["status"] == "restarted"
    assert payload["resumed"] is True
    assert payload["previous_conversation_id"] == "ses_backfilled123"
    assert stored_id == "ses_backfilled123"
    assert sent_raw == ["C-c", "/exit", "Enter"]
    assert recreated[0][0] == "opencode-backfill"
    assert "--session ses_backfilled123" in recreated[0][1]
    assert "--model openai/gpt-5.4" in recreated[0][1]
    assert '"permission":"allow"' in recreated[0][1]
    assert "tui.dark.json" in recreated[0][1]
