from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fastapi import HTTPException
from starlette.requests import Request

pytest_plugins = ["anyio"]

API_TOKEN = "test-api-token-share"

@pytest.fixture
def tmp_db(tmp_path: Path) -> str:
    from core.api.tests._db_fixture import apply_migrations

    db_path = str(tmp_path / "test.db")
    apply_migrations(db_path)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute(
        "INSERT OR IGNORE INTO users (id, slug, display_name, type, system_role, created_at, updated_at) "
        "VALUES ('usr_marvisx', 'marvisx', 'MarvisX', 'agent', 'operator', datetime('now'), datetime('now'))"
    )
    conn.execute(
        "INSERT OR IGNORE INTO users (id, slug, display_name, type, system_role, created_at, updated_at) "
        "VALUES ('usr_viewerbot', 'viewerbot', 'Viewer Bot', 'agent', 'viewer', datetime('now'), datetime('now'))"
    )
    conn.commit()
    conn.close()
    return db_path


@pytest.fixture
def tmp_repo(tmp_path: Path) -> Path:
    repo_root = tmp_path / "workspace"
    (repo_root / "docs").mkdir(parents=True)
    (repo_root / "docs" / "report.md").write_text(
        "# Repo Share Report\n\nThis file is shared from the workspace repo.\n",
        encoding="utf-8",
    )
    return repo_root


@pytest.fixture
def share_repo_module(tmp_repo: Path):
    from core.api.config import Settings
    import core.api.routers.finder as finder
    import core.api.routers.share_repo as share_repo
    import core.api.services.share_links as share_links

    test_settings = Settings(
        pir_env="test",
        db_path=":memory:",
        finder_root=str(tmp_repo.parent),
        tasks_api_token=API_TOKEN,
        repo_share_root=str(tmp_repo),
        telegram_bot_token="",
        telegram_owner_chat_id="",
    )

    finder.settings = test_settings
    share_repo.settings = test_settings
    share_links.settings = test_settings
    return finder, share_repo, share_links


def _user_info(user_id: str, slug: str, role: str):
    from core.api.models import UserInfo

    return UserInfo(
        username=slug,
        user_id=user_id,
        system_role=role,
        user_type="agent",
        display_name=slug,
        workspace_id="ws_default",
    )


class _FakeAsyncCursor:
    def __init__(self, cursor: sqlite3.Cursor):
        self._cursor = cursor

    async def fetchone(self):
        return self._cursor.fetchone()

    async def fetchall(self):
        return self._cursor.fetchall()


class _FakeAsyncDB:
    def __init__(self, connection: sqlite3.Connection):
        self._connection = connection

    async def execute(self, sql: str, params=()):
        return _FakeAsyncCursor(self._connection.execute(sql, params))

    async def commit(self):
        self._connection.commit()


@pytest.mark.anyio
async def test_operator_can_share_workspace_markdown_via_unified_endpoint(share_repo_module, tmp_db: str):
    finder, _, _ = share_repo_module
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/v1/shared/test",
            "headers": [],
            "query_string": b"",
        }
    )

    connection = sqlite3.connect(tmp_db)
    connection.row_factory = sqlite3.Row
    db = _FakeAsyncDB(connection)
    try:
        payload = await finder.create_share_link(
            {"path": "workspace/docs/report.md", "hours": 12},
            current_user=_user_info("usr_marvisx", "marvisx", "operator"),
            db=db,
        )
        assert payload["path"] == "workspace/docs/report.md"
        assert payload["url"].startswith("/api/v1/shared/")

        public_response = await finder.access_shared_file(
            payload["token"],
            request=request,
            db=db,
        )
        assert public_response.status_code == 200
        assert public_response.media_type == "text/html"
        body = public_response.body.decode("utf-8")
        assert "Repo Share Report" in body
        assert "This file is shared from the workspace repo." in body
    finally:
        connection.close()


@pytest.mark.anyio
async def test_viewer_cannot_create_repo_share():
    with pytest.raises(HTTPException) as exc_info:
        from core.api.services.share_links import enforce_workspace_share_role
        enforce_workspace_share_role(_user_info("usr_viewerbot", "viewerbot", "viewer"))

    assert exc_info.value.status_code == 403


@pytest.mark.anyio
async def test_repo_share_blocks_path_traversal(share_repo_module, tmp_db: str):
    finder, share_repo, _ = share_repo_module
    connection = sqlite3.connect(tmp_db)
    connection.row_factory = sqlite3.Row
    db = _FakeAsyncDB(connection)
    try:
        with pytest.raises(HTTPException) as exc_info:
            await finder.create_share_link(
                {"path": "workspace/../secrets.txt", "hours": 12},
                current_user=_user_info("usr_marvisx", "marvisx", "operator"),
                db=db,
            )
    finally:
        connection.close()

    assert exc_info.value.status_code == 403

    connection = sqlite3.connect(tmp_db)
    connection.row_factory = sqlite3.Row
    db = _FakeAsyncDB(connection)
    try:
        payload = await share_repo.create_repo_share_link(
            {"path": "workspace/docs/report.md", "hours": 12},
            current_user=_user_info("usr_marvisx", "marvisx", "operator"),
            db=db,
        )
        assert payload["url"].startswith("/api/v1/shared/")
    finally:
        connection.close()
