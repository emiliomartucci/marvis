from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timedelta, timezone
import importlib
from pathlib import Path
import sqlite3
import threading

import aiosqlite
import pytest

from core.api.mcp.tools import projects as project_tools
from core.api.services import project_lifecycle as lifecycle
from core.api.tests._db_fixture import apply_migrations
from core.api.use_cases._context import CallerContext
from core.api.use_cases._errors import AuthorizationError, ConflictError


workspace = importlib.import_module("core.api.mcp.tools.workspace")


class _FakeMcp:
    def __init__(self) -> None:
        self.tools: dict[str, object] = {}

    def tool(self):
        def decorator(function):
            self.tools[function.__name__] = function
            return function

        return decorator


def _admin() -> CallerContext:
    return CallerContext(
        username="local-admin",
        user_id="local-admin",
        system_role="admin",
        user_type="human",
        workspace_id="ws_default",
        is_human_session=True,
    )


def _project_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Path, Path]:
    projects_root = tmp_path / "projects"
    project = projects_root / "sample"
    project.mkdir(parents=True)
    (project / "project.yaml").write_text(
        "name: Sample\nslug: sample\ntype: work\nlifecycle: active\n",
        encoding="utf-8",
    )
    (project / "memory").mkdir()
    (project / "memory" / "history.md").write_text("history\n", encoding="utf-8")
    monkeypatch.setenv("MARVIS_PROJECTS_ROOT", str(projects_root))
    db_path = tmp_path / "console.db"
    apply_migrations(str(db_path))
    return projects_root, db_path


def test_migration_187_installs_and_seals_lifecycle_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _projects_root, db_path = _project_database(tmp_path, monkeypatch)
    with sqlite3.connect(db_path) as connection:
        version = connection.execute(
            "SELECT MAX(version) FROM schema_versions"
        ).fetchone()[0]
        objects = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table','trigger')"
            )
        }
        marker = connection.execute(
            "SELECT state,project_count,snapshot_digest "
            "FROM project_lifecycle_bootstrap WHERE id=1"
        ).fetchone()

    assert version == 187
    assert {
        "project_lifecycle_state",
        "project_write_events",
        "cloud_f_control",
        "governed_decisions",
        "project_write_events_writability_gate",
        "project_write_events_advance_watermark",
    } <= objects
    assert marker[0] == "complete"
    assert marker[1] == 1
    assert len(marker[2]) == 64


@pytest.mark.asyncio
async def test_archive_is_idempotent_and_fences_late_writers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    projects_root, db_path = _project_database(tmp_path, monkeypatch)
    db = await aiosqlite.connect(db_path)
    db.row_factory = aiosqlite.Row
    try:
        ctx = _admin()
        state = await lifecycle.read_project_lifecycle(
            db,
            workspace_id=ctx.workspace_id,
            project_slug="sample",
        )
        await lifecycle.ensure_cloud_f_control(db, workspace_id=ctx.workspace_id)
        await db.commit()
        control = await lifecycle.activate_cloud_f_control(
            ctx,
            db,
            subtype="bootstrap_activation",
            expected_epoch=0,
        )
        approval = await lifecycle.create_archive_approval(
            ctx,
            db,
            project_slug="sample",
            expected_project_id=state.project_id,
            expected_project_digest=lifecycle.project_digest(projects_root / "sample"),
            plan_f_digest="1" * 64,
            master_digest="2" * 64,
            evidence_digest="3" * 64,
            expected_writer_watermark=state.writer_watermark,
            expected_selector_watermark=state.selector_watermark,
            expected_cloud_f_epoch=control.change_epoch,
            expected_active_operations_digest=lifecycle._EMPTY_ACTIVE_OPERATIONS_DIGEST,
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
            projects_root=projects_root,
        )
        arguments = {
            "project_slug": "sample",
            "project_id": state.project_id,
            "approval_id": approval["approval_id"],
            "expected_project_digest": approval["expected_project_digest"],
            "plan_f_digest": "1" * 64,
            "master_digest": "2" * 64,
            "evidence_digest": "3" * 64,
            "expected_writer_watermark": state.writer_watermark,
            "expected_selector_watermark": state.selector_watermark,
            "expected_cloud_f_epoch": control.change_epoch,
            "expected_active_operations_digest": lifecycle._EMPTY_ACTIVE_OPERATIONS_DIGEST,
            "operation_id": "op-archive-sample",
            "idempotency_key": "idem-archive-sample",
            "lease_expires_at": datetime.now(timezone.utc) + timedelta(minutes=10),
            "projects_root": projects_root,
        }
        archived = await lifecycle.archive_project(ctx, db, **arguments)
        replay = await lifecycle.archive_project(ctx, db, **arguments)

        assert replay == archived
        assert "lifecycle: archived" in (
            projects_root / "sample" / "project.yaml"
        ).read_text(encoding="utf-8")
        with pytest.raises(aiosqlite.IntegrityError, match="project_not_writable"):
            await db.execute(
                "INSERT INTO tasks"
                "(id,title,status,project,priority,created_by,source,workspace_id) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (
                    "late-write",
                    "Late write",
                    "pending",
                    "sample",
                    "medium",
                    "agent",
                    "test",
                    "ws_default",
                ),
            )
        await db.rollback()
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_expired_cloud_f_lease_requires_exact_idempotent_renewal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    projects_root, db_path = _project_database(tmp_path, monkeypatch)
    db = await aiosqlite.connect(db_path)
    db.row_factory = aiosqlite.Row
    try:
        ctx = _admin()
        state = await lifecycle.read_project_lifecycle(
            db,
            workspace_id=ctx.workspace_id,
            project_slug="sample",
        )
        await lifecycle.ensure_cloud_f_control(db, workspace_id=ctx.workspace_id)
        await db.commit()
        control = await lifecycle.activate_cloud_f_control(
            ctx,
            db,
            subtype="bootstrap_activation",
            expected_epoch=0,
        )
        await lifecycle.acquire_cloud_f_change(
            ctx,
            db,
            operation_id="op-selector",
            operation_kind="selector_update",
            expected_epoch=control.change_epoch,
            lease_expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        )
        await db.execute(
            "UPDATE cloud_f_control SET lease_expires_at='2000-01-01T00:00:00Z' "
            "WHERE workspace_id=?",
            (ctx.workspace_id,),
        )
        await db.commit()

        with pytest.raises(ConflictError) as expired:
            await lifecycle.update_project_selector_watermark(
                ctx,
                db,
                project_slug="sample",
                operation_id="op-selector",
                expected_epoch=control.change_epoch,
                expected_selector_watermark=state.selector_watermark,
                selector_watermark="b" * 64,
                projects_root=projects_root,
            )
        assert expired.value.code == "cloud_f_lease_expired"

        renewed = await lifecycle.acquire_cloud_f_change(
            ctx,
            db,
            operation_id="op-selector",
            operation_kind="selector_update",
            expected_epoch=control.change_epoch,
            lease_expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        )
        assert renewed.lease_operation_id == "op-selector"
        with pytest.raises(ConflictError) as changed_coordinates:
            await lifecycle.acquire_cloud_f_change(
                ctx,
                db,
                operation_id="op-selector",
                operation_kind="different_kind",
                expected_epoch=control.change_epoch,
                lease_expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
            )
        assert changed_coordinates.value.code == "cloud_f_operation_conflict"
    finally:
        await db.close()


def test_http_and_mcp_expose_same_governed_decision_lifecycle() -> None:
    from fastapi.routing import APIRoute

    from core.api.routers import projects as projects_router

    routes = {
        (method, route.path)
        for route in projects_router.router.routes
        if isinstance(route, APIRoute)
        for method in route.methods
    }
    assert {
        ("POST", "/api/v1/projects/{slug}/decisions"),
        ("GET", "/api/v1/projects/{slug}/decisions/{decision_id}"),
        ("POST", "/api/v1/projects/{slug}/decisions/{decision_id}/accept"),
        ("POST", "/api/v1/projects/{slug}/decisions/{decision_id}/supersede"),
        ("POST", "/api/v1/projects/{slug}/historical-pointers"),
        ("GET", "/api/v1/projects/{slug}/historical-pointers"),
    } <= routes

    mcp = _FakeMcp()
    project_tools.register(mcp)
    assert {
        "create_governed_decision",
        "get_governed_decision",
        "accept_governed_decision",
        "supersede_governed_decision",
        "create_historical_pointer",
        "list_historical_pointers",
    } <= set(mcp.tools)


@pytest.mark.asyncio
async def test_mcp_lifecycle_registration_rejects_viewer_before_discovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mcp = _FakeMcp()
    project_tools.register(mcp)
    viewer = CallerContext(
        username="viewer",
        user_id="usr-viewer",
        system_role="viewer",
        workspace_id="ws-a",
    )

    @asynccontextmanager
    async def acquire_write_db(*, label: str):
        assert label == "mcp.register_project_lifecycle"
        yield object()

    async def forbidden_discovery(*_args, **_kwargs):
        raise AssertionError("viewer reached project discovery")

    def tool_error(error):
        raise RuntimeError(error.code)

    monkeypatch.setattr(project_tools, "acquire_write_db", acquire_write_db)
    monkeypatch.setattr(project_tools, "current_mcp_context", lambda: viewer)
    monkeypatch.setattr(project_tools, "current_visible_projects", forbidden_discovery)
    monkeypatch.setattr(project_tools, "raise_mcp_error", tool_error)

    with pytest.raises(RuntimeError, match="insufficient_permissions"):
        await mcp.tools["register_project_lifecycle"]("sample")


def test_real_project_lock_releases_on_different_worker_thread(tmp_path: Path) -> None:
    lock_path = tmp_path / "cross-thread.lock"
    lock = lifecycle.exclusive_file_lock(
        lock_path,
        mode=0o600,
        nofollow=True,
        thread_local=False,
    )

    def enter() -> int:
        lock.__enter__()
        return threading.get_ident()

    def exit_lock() -> int:
        lock.__exit__(None, None, None)
        return threading.get_ident()

    with (
        ThreadPoolExecutor(max_workers=1) as acquire_pool,
        ThreadPoolExecutor(max_workers=1) as release_pool,
    ):
        acquire_thread = acquire_pool.submit(enter).result(timeout=2)
        release_thread = release_pool.submit(exit_lock).result(timeout=2)

    assert acquire_thread != release_thread
    with lifecycle.exclusive_file_lock(
        lock_path,
        mode=0o600,
        nofollow=True,
        thread_local=False,
    ):
        pass


@pytest.mark.asyncio
async def test_cancelled_async_lock_releases_after_delayed_acquisition(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    acquisition_started = threading.Event()
    allow_acquisition = threading.Event()
    released = threading.Event()
    body_entered = asyncio.Event()

    @contextmanager
    def delayed_lock(
        _path: Path,
        *,
        mode: int,
        nofollow: bool,
        thread_local: bool,
    ):
        assert (mode, nofollow, thread_local) == (0o600, True, False)
        acquisition_started.set()
        assert allow_acquisition.wait(timeout=2)
        try:
            yield
        finally:
            released.set()

    monkeypatch.setattr(lifecycle, "exclusive_file_lock", delayed_lock)

    async def waiter() -> None:
        async with lifecycle.async_project_mutation_guard(
            projects_root=tmp_path / "projects"
        ):
            body_entered.set()

    task = asyncio.create_task(waiter())
    assert await asyncio.to_thread(acquisition_started.wait, 1)
    task.cancel()
    allow_acquisition.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await asyncio.to_thread(released.wait, 1)
    assert not body_entered.is_set()


def test_workspace_rejects_project_lifecycle_metadata() -> None:
    for path in (
        "sample/project.yaml",
        "sample/PROJECT.YAML",
        "projects/sample/project.yaml",
        "Projects/sample/Project.Yaml",
    ):
        with pytest.raises(AuthorizationError) as denied:
            workspace._reject_project_lifecycle_path(path)
        assert denied.value.code == "project_lifecycle_path_denied"


@pytest.mark.asyncio
async def test_workspace_run_bash_fails_before_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mcp = _FakeMcp()
    workspace.register(mcp)

    def propagate(error):
        raise error

    def forbidden_service(*_args, **_kwargs):
        raise AssertionError("run_bash service was called")

    monkeypatch.setattr(workspace, "raise_mcp_error", propagate)
    monkeypatch.setattr(workspace.svc, "run_bash", forbidden_service)
    with pytest.raises(AuthorizationError) as denied:
        await mcp.tools["run_bash"]("touch note.md")
    assert denied.value.code == "project_lifecycle_shell_denied"


@pytest.mark.asyncio
async def test_workspace_archived_project_write_is_fenced(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _projects_root, db_path = _project_database(tmp_path, monkeypatch)
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE project_lifecycle_state SET lifecycle='archived',"
            "archived_at='2026-08-29T00:00:00Z' "
            "WHERE workspace_id='ws_default' AND project_slug='sample'"
        )
        connection.commit()

    @asynccontextmanager
    async def acquire_write_db(*, label: str):
        assert label == "mcp.workspace.workspace_file"
        db = await aiosqlite.connect(db_path)
        db.row_factory = aiosqlite.Row
        try:
            yield db
        finally:
            await db.close()

    monkeypatch.setattr(workspace, "acquire_write_db", acquire_write_db)
    with pytest.raises(ConflictError) as denied:
        async with workspace._project_file_mutation(
            "sample/note.md",
            writer_kind="workspace_file",
        ):
            raise AssertionError("archived project write reached filesystem effect")
    assert denied.value.code == "project_not_writable"


@pytest.mark.asyncio
async def test_workspace_write_adapters_use_lifecycle_fence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[tuple[str, str]] = []

    @asynccontextmanager
    async def mutation(path: str, *, writer_kind: str):
        seen.append((path, writer_kind))
        yield None

    monkeypatch.setattr(workspace, "_project_file_mutation", mutation)
    monkeypatch.setattr(workspace.svc, "write_file", lambda *_args, **_kwargs: {"ok": True})
    monkeypatch.setattr(workspace.svc, "edit", lambda *_args, **_kwargs: {"ok": True})
    mcp = _FakeMcp()
    workspace.register(mcp)

    await mcp.tools["write_file"]("sample/a.md", "body")
    await mcp.tools["edit"]("sample/a.md", "body", "updated")

    assert seen == [
        ("sample/a.md", "workspace_file"),
        ("sample/a.md", "workspace_edit"),
    ]
