"""Tests for /shared/<token> JSON + PUT edit endpoints."""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest
from fastapi import HTTPException
from starlette.requests import Request

pytest_plugins = ["anyio"]

API_TOKEN = "test-api-token-share-edit"


@pytest.fixture
def tmp_db(tmp_path: Path) -> str:
    from core.api.tests._db_fixture import apply_migrations

    db_path = str(tmp_path / "test.db")
    apply_migrations(db_path)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute(
        "INSERT OR IGNORE INTO users (id, slug, display_name, type, system_role, "
        "created_at, updated_at) "
        "VALUES ('usr_admin', 'admin', 'Admin', 'human', 'super_admin', "
        "datetime('now'), datetime('now'))"
    )
    conn.execute(
        "INSERT OR IGNORE INTO users (id, slug, display_name, type, system_role, "
        "created_at, updated_at) "
        "VALUES ('usr_operator', 'operator', 'Operator', 'agent', 'operator', "
        "datetime('now'), datetime('now'))"
    )
    conn.commit()
    conn.close()
    return db_path


@pytest.fixture
def tmp_repo(tmp_path: Path) -> Path:
    repo_root = tmp_path / "workspace"
    (repo_root / "docs").mkdir(parents=True)
    (repo_root / "docs" / "report.md").write_text(
        "# Original content\n", encoding="utf-8"
    )
    return repo_root


@pytest.fixture
def share_modules(tmp_repo: Path):
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
        user_type="agent" if role != "super_admin" else "human",
        display_name=slug,
        workspace_id="ws_default",
    )


def _local_user_info():
    from core.api.models import UserInfo

    return UserInfo(
        username="local",
        user_id="local",
        system_role="operator",
        user_type="human",
        display_name="Local Operator",
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


def _make_request(query: bytes = b"") -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/v1/shared/test",
            "headers": [],
            "query_string": query,
        }
    )


async def _create_share(finder, db, *, user, path: str = "workspace/docs/report.md") -> dict:
    return await finder.create_share_link(
        {"path": path, "hours": 12},
        current_user=user,
        db=db,
    )


# --- GET ?format=json ---


@pytest.mark.anyio
async def test_get_json_unauthenticated_returns_no_can_edit(
    share_modules, tmp_db: str
) -> None:
    """Unauthenticated GET ?format=json: is_authenticated=False, can_edit=False."""
    finder, _, _ = share_modules
    connection = sqlite3.connect(tmp_db)
    connection.row_factory = sqlite3.Row
    db = _FakeAsyncDB(connection)
    try:
        op = _user_info("usr_operator", "operator", "operator")
        payload = await _create_share(finder, db, user=op)

        result = await finder.access_shared_file(
            payload["token"],
            request=_make_request(b"format=json"),
            format="json",
            db=db,
        )
        assert result["filename"] == "report.md"
        assert result["is_authenticated"] is False
        assert result["can_edit"] is False
        assert result["editable"] is True  # workspace share
        assert "Original content" in result["content"]
    finally:
        connection.close()


@pytest.mark.anyio
async def test_get_json_authenticated_local_workspace_share_can_edit(
    share_modules, tmp_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Trusted local OSS operator can edit its host-local workspace share."""
    finder, _, _ = share_modules
    connection = sqlite3.connect(tmp_db)
    connection.row_factory = sqlite3.Row
    db = _FakeAsyncDB(connection)
    local = _local_user_info()

    async def _fake_auth(request: Request, db: Any) -> Any:
        return local

    monkeypatch.setattr(finder, "_try_get_authenticated_user", _fake_auth)

    try:
        payload = await _create_share(finder, db, user=local)
        result = await finder.access_shared_file(
            payload["token"],
            request=_make_request(b"format=json"),
            format="json",
            db=db,
        )
        assert result["is_authenticated"] is True
        assert result["editable"] is True
        assert result["can_edit"] is True
    finally:
        connection.close()


@pytest.mark.anyio
async def test_get_json_authenticated_operator_workspace_share_no_visibility(
    share_modules, tmp_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Operator on workspace/ share: visibility check fails (non-admin can only
    see projects/), so can_edit=False even though authenticated + workspace share.
    """
    finder, _, _ = share_modules
    connection = sqlite3.connect(tmp_db)
    connection.row_factory = sqlite3.Row
    db = _FakeAsyncDB(connection)
    op = _user_info("usr_operator", "operator", "operator")

    async def _fake_auth(request: Request, db: Any) -> Any:
        return op

    monkeypatch.setattr(finder, "_try_get_authenticated_user", _fake_auth)

    try:
        payload = await _create_share(finder, db, user=op)
        result = await finder.access_shared_file(
            payload["token"],
            request=_make_request(b"format=json"),
            format="json",
            db=db,
        )
        assert result["is_authenticated"] is True
        assert result["editable"] is True
        # Operator cannot reach workspace/ via visibility (non-admin → projects/ only).
        assert result["can_edit"] is False
    finally:
        connection.close()


# --- PUT save ---


@pytest.mark.anyio
async def test_put_unauthenticated_rejected(share_modules, tmp_db: str) -> None:
    """PUT without auth → save_shared_file is dependency-protected; calling it
    directly with no current_user simulates the FastAPI 401 path. We assert via
    explicit call that visibility/role logic blocks the write end-to-end when
    a viewer slips through."""
    finder, _, _ = share_modules
    from core.api.models import FinderFileUpdate

    connection = sqlite3.connect(tmp_db)
    connection.row_factory = sqlite3.Row
    db = _FakeAsyncDB(connection)
    try:
        op = _user_info("usr_operator", "operator", "operator")
        payload = await _create_share(finder, db, user=op)
        viewer = _user_info("usr_viewer", "viewer", "viewer")
        with pytest.raises(HTTPException) as exc:
            await finder.save_shared_file(
                payload["token"],
                body=FinderFileUpdate(content="hacked"),
                request=_make_request(),
                current_user=viewer,
                db=db,
            )
        # viewer fails _check_finder_visibility default-deny → 404
        # (was 403 from workspace-only gate, removed in PR #25 when scope
        # widened to /data project shares).
        assert exc.value.status_code == 404
    finally:
        connection.close()


@pytest.mark.anyio
async def test_put_local_workspace_share_writes_file(
    share_modules, tmp_db: str, tmp_repo: Path
) -> None:
    """Trusted local OSS operator can save a host-local workspace share."""
    finder, _, _ = share_modules
    from core.api.models import FinderFileUpdate

    connection = sqlite3.connect(tmp_db)
    connection.row_factory = sqlite3.Row
    db = _FakeAsyncDB(connection)
    local = _local_user_info()
    try:
        payload = await _create_share(finder, db, user=local)
        new_content = "# Edited via shared PUT\n\nNew body.\n"
        result = await finder.save_shared_file(
            payload["token"],
            body=FinderFileUpdate(content=new_content),
            request=_make_request(),
            current_user=local,
            db=db,
        )
        assert result["filename"] == "report.md"
        assert result["size"] == len(new_content.encode("utf-8"))
        on_disk = (tmp_repo / "docs" / "report.md").read_text(encoding="utf-8")
        assert on_disk == new_content
    finally:
        connection.close()


@pytest.mark.anyio
async def test_put_operator_workspace_share_blocked_by_visibility(
    share_modules, tmp_db: str, tmp_repo: Path
) -> None:
    """Operator hitting PUT on workspace/ share is blocked by visibility check
    (parts[0]=='workspace' != 'projects'). File on disk must remain untouched.
    """
    finder, _, _ = share_modules
    from core.api.models import FinderFileUpdate

    connection = sqlite3.connect(tmp_db)
    connection.row_factory = sqlite3.Row
    db = _FakeAsyncDB(connection)
    op = _user_info("usr_operator", "operator", "operator")
    try:
        payload = await _create_share(finder, db, user=op)
        original = (tmp_repo / "docs" / "report.md").read_text(encoding="utf-8")
        with pytest.raises(HTTPException) as exc:
            await finder.save_shared_file(
                payload["token"],
                body=FinderFileUpdate(content="should not write"),
                request=_make_request(),
                current_user=op,
                db=db,
            )
        assert exc.value.status_code == 404  # visibility default-deny → 404
        on_disk = (tmp_repo / "docs" / "report.md").read_text(encoding="utf-8")
        assert on_disk == original
    finally:
        connection.close()


@pytest.mark.anyio
async def test_put_data_project_share_local_operator_writes(
    share_modules, tmp_db: str, tmp_path: Path
) -> None:
    """Trusted local OSS operator can save a /data project share.

    Caso piu' comune via mcp_share su file metadata di progetto:
    `projects/<slug>/docs/foo.md`. Pre-PR#25 era 403 "Public shares are
    read-only"; the host-local route still supports the OSS single-user flow.
    """
    finder, _, _ = share_modules
    from core.api.models import FinderFileUpdate

    # Setup: file in projects/<slug>/foo.md sotto finder_root
    finder_root = tmp_path
    project_dir = finder_root / "projects" / "demo"
    project_dir.mkdir(parents=True)
    target = project_dir / "report.md"
    target.write_text("# Original\n", encoding="utf-8")

    # Punta finder_root al tmp_path (parent di projects/)
    from core.api.config import Settings
    test_settings = Settings(
        pir_env="test",
        db_path=":memory:",
        finder_root=str(finder_root),
        tasks_api_token=API_TOKEN,
        repo_share_root=str(finder_root / "workspace"),
        telegram_bot_token="",
        telegram_owner_chat_id="",
    )
    finder.settings = test_settings

    connection = sqlite3.connect(tmp_db)
    connection.row_factory = sqlite3.Row
    db = _FakeAsyncDB(connection)
    local = _local_user_info()
    try:
        # Insert share riga direttamente con path projects/demo/report.md
        connection.execute(
            "INSERT INTO shared_links (token, path, created_by, expires_at) "
            "VALUES (?, ?, ?, datetime('now', '+1 day'))",
            ("data-token", "projects/demo/report.md", "local"),
        )
        connection.commit()

        result = await finder.save_shared_file(
            "data-token",
            body=FinderFileUpdate(content="# Edited via /data share PUT\n"),
            request=_make_request(),
            current_user=local,
            db=db,
        )
        assert result["filename"] == "report.md"
        assert "Edited via /data share PUT" in target.read_text(encoding="utf-8")
    finally:
        connection.close()
