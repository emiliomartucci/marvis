from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fastapi import HTTPException

from core.api.models.auth import UserInfo
from core.api.routers.audit import list_audit_entries


class FakeAsyncCursor:
    def __init__(self, cursor: sqlite3.Cursor) -> None:
        self._cursor = cursor

    async def fetchall(self):
        return self._cursor.fetchall()


class FakeAsyncDB:
    def __init__(self, path: str) -> None:
        self._conn = sqlite3.connect(path)
        self._conn.row_factory = sqlite3.Row

    async def execute(self, query: str, params):
        cursor = self._conn.execute(query, params)
        return FakeAsyncCursor(cursor)

    def close(self) -> None:
        self._conn.close()


@pytest.fixture
def tmp_db(tmp_path: Path) -> str:
    db_path = str(tmp_path / "test.db")

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE audit_log ("
        "id TEXT PRIMARY KEY, "
        "timestamp TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')), "
        "action TEXT NOT NULL, "
        "user TEXT NOT NULL, "
        "resource_type TEXT NOT NULL, "
        "resource_id TEXT NOT NULL, "
        "details_json TEXT, "
        "workspace_id TEXT NOT NULL)"
    )

    conn.execute(
        "INSERT INTO audit_log (id, action, user, resource_type, resource_id, details_json, "
        "workspace_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            "a1",
            "check_learnings",
            "agent:rem",
            "learning",
            "l1,l2",
            '{"query":"worktree"}',
            "ws_default",
        ),
    )
    conn.execute(
        "INSERT INTO audit_log (id, action, user, resource_type, resource_id, details_json, "
        "workspace_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            "a2",
            "task.approve",
            "emilio",
            "task",
            "t1",
            '{"status":"approved"}',
            "ws_default",
        ),
    )
    conn.commit()
    conn.close()
    return db_path


@pytest.mark.asyncio
async def test_operator_can_read_check_learnings_audit(tmp_db: str):
    db = FakeAsyncDB(tmp_db)
    try:
        payload = await list_audit_entries(
            action="check_learnings",
            user=None,
            resource_type=None,
            resource_id=None,
            limit=10,
            offset=0,
            current_user=UserInfo(username="agent:rem", system_role="operator", user_type="agent"),
            db=db,
        )
    finally:
        db.close()

    assert len(payload) == 1
    assert payload[0].action == "check_learnings"


@pytest.mark.asyncio
async def test_operator_cannot_read_general_audit(tmp_db: str):
    db = FakeAsyncDB(tmp_db)
    try:
        with pytest.raises(HTTPException) as exc_info:
            await list_audit_entries(
                action=None,
                user=None,
                resource_type=None,
                resource_id=None,
                limit=10,
                offset=0,
                current_user=UserInfo(
                    username="agent:rem",
                    system_role="operator",
                    user_type="agent",
                ),
                db=db,
            )
    finally:
        db.close()

    assert exc_info.value.status_code == 403
    assert exc_info.value.detail == "Insufficient permissions"


@pytest.mark.asyncio
async def test_admin_keeps_full_audit_access(tmp_db: str):
    db = FakeAsyncDB(tmp_db)
    try:
        payload = await list_audit_entries(
            action=None,
            user=None,
            resource_type=None,
            resource_id=None,
            limit=10,
            offset=0,
            current_user=UserInfo(username="emilio", system_role="admin", user_type="human"),
            db=db,
        )
    finally:
        db.close()

    assert len(payload) == 2
    assert {entry.action for entry in payload} == {"check_learnings", "task.approve"}
