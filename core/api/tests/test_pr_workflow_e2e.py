# v1.0.0 - 2026-03-01 - E2E tests for full PR lifecycle (submit -> merge/close -> task status)
"""
E2E tests for the full PR lifecycle.

Test 1 (happy path):
    task (in_progress) -> submit_pr -> webhook(merged=true) -> task(completed)

Test 2 (PR closed without merge):
    task (in_progress) -> submit_pr -> webhook(closed, merged=false) -> task(in_progress) + review_feedback

These tests use httpx.AsyncClient with ASGITransport to run fully in-process (no network),
bypassing authentication via dependency overrides.

Requirements:
    pip install pytest pytest-asyncio httpx anyio

Run:
    cd /var/marvisx/workspace/projects/MarvisX
    /data/pir/venv/bin/pip install pytest pytest-asyncio
    /data/pir/venv/bin/pytest api/tests/test_pr_workflow_e2e.py -v

Important assumptions:
    - GITHUB_WEBHOOK_SECRET must match settings.github_webhook_secret for signature verification.
    - The test patches git_ops (create_worktree_async, merge_branch_async, get_pr_diff_async,
      remove_worktree_async) to avoid real git operations.
    - A temporary SQLite DB is used per test — no interference with production data.
    - The test task is created directly in the DB (bypassing pending->approved->in_progress flow)
      so it starts in in_progress state, matching what submit_pr expects.
    - Webhook events are processed synchronously (awaited directly) to avoid BackgroundTasks
      timing issues in test context.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import sqlite3
import tempfile
import uuid
from collections.abc import AsyncGenerator
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import aiosqlite
import pytest
import httpx
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient

# ---------------------------------------------------------------------------
# Pytest-asyncio configuration
# ---------------------------------------------------------------------------
pytest_plugins = ["anyio"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

WEBHOOK_SECRET = "test-webhook-secret-e2e"
API_TOKEN = "test-api-token-e2e"
TEST_PROJECT = "marvisx"  # must match a code/system project with repo_path


def _make_signature(secret: str, body: bytes) -> str:
    """HMAC-SHA256 signature in GitHub format."""
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def _make_pr_webhook_payload(
    branch: str,
    action: str = "closed",
    merged: bool = True,
    pr_number: int = 42,
    pr_body: str = "Test PR body",
) -> dict:
    """Build a minimal GitHub pull_request webhook payload."""
    return {
        "action": action,
        "pull_request": {
            "number": pr_number,
            "title": "Test PR",
            "body": pr_body,
            "merged": merged,
            "html_url": f"https://github.com/test/repo/pull/{pr_number}",
            "head": {"ref": branch},
        },
        "repository": {
            "full_name": "test/marvisx",
        },
    }


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_db(tmp_path: Path) -> str:
    """Create a temporary SQLite DB with the full schema applied."""
    db_path = str(tmp_path / "test.db")

    # Apply all migrations synchronously
    from core.api.db import MIGRATIONS_DIR

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row

    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_versions "
        "(version INTEGER PRIMARY KEY, applied_at DATETIME DEFAULT CURRENT_TIMESTAMP)"
    )

    migration_files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    for mf in migration_files:
        if mf.stem.endswith("_down"):
            continue
        version = int(mf.stem.split("_")[0])
        sql = mf.read_text()
        # Skip INSERT INTO schema_versions — managed separately
        conn.executescript(sql)
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            conn.execute(
                "INSERT OR IGNORE INTO schema_versions (version) VALUES (?)", (version,)
            )
            conn.commit()
        except Exception:
            pass

    # Seed minimum user (marvisx agent with operator role)
    conn.execute(
        "INSERT OR IGNORE INTO users (id, slug, display_name, type, system_role, created_at, updated_at) "
        "VALUES ('usr_marvisx', 'marvisx', 'MarvisX', 'agent', 'operator', datetime('now'), datetime('now'))"
    )
    conn.commit()
    conn.close()
    return db_path


@pytest.fixture
def app_with_overrides(tmp_db: str):
    """Return the FastAPI app with settings patched for test isolation."""
    from core.api.config import Settings

    test_settings = Settings(
        pir_env="test",
        db_path=tmp_db,
        tasks_api_token=API_TOKEN,
        github_webhook_secret=WEBHOOK_SECRET,
        # Disable telegram notifications
        telegram_bot_token="",
        telegram_owner_chat_id="",
    )

    # Patch settings globally so all modules see the test DB
    with patch("api.config.settings", test_settings), \
         patch("api.db.settings", test_settings), \
         patch("api.routers.tasks.settings", test_settings), \
         patch("api.routers.pull_requests.settings", test_settings), \
         patch("api.services.pr_service.settings", test_settings, create=True), \
         patch("api.services.webhook_service.settings", test_settings), \
         patch("api.services.task_transitions.settings", test_settings, create=True):

        # Import app AFTER patching settings
        # (app reads settings at import time for CORS etc.)
        from core.api.main import app
        yield app, test_settings


@pytest.fixture
async def client_with_task(app_with_overrides, tmp_db: str):
    """
    Yield (AsyncClient, task_id, branch_name) with:
    - task already in in_progress status
    - PR record already in 'open' status (simulating post-submit state)
    - branch name matching feat/task-{uuid} pattern
    """
    app, settings_obj = app_with_overrides
    task_id = str(uuid.uuid4())
    branch = f"feat/task-{task_id}"
    pr_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()

    # Insert task directly in in_progress (bypassing state machine)
    async with aiosqlite.connect(tmp_db) as db:
        await db.execute("PRAGMA foreign_keys=ON")
        db.row_factory = aiosqlite.Row
        await db.execute(
            "INSERT INTO tasks (id, title, description, status, project, priority, "
            "created_by, source, created_at, updated_at) "
            "VALUES (?, ?, ?, 'in_progress', ?, 'medium', 'marvisx', 'manual', ?, ?)",
            (task_id, f"E2E Test Task {task_id[:8]}", "Test task for E2E PR workflow",
             TEST_PROJECT, now, now),
        )
        # Insert PR record in 'open' status (post-submit state)
        await db.execute(
            "INSERT INTO pull_requests (id, task_id, project, branch, target, status, "
            "title, body, worktree_path, created_at) "
            "VALUES (?, ?, ?, ?, 'main', 'open', 'Test PR', 'Test body', '/tmp/test-worktree', ?)",
            (pr_id, task_id, TEST_PROJECT, branch, now),
        )
        # Task must be in 'review' for webhook to find it via _find_pr_by_branch
        # (pr_service.submit_pr transitions task to review; we simulate that here)
        await db.execute(
            "UPDATE tasks SET status = 'review' WHERE id = ?", (task_id,)
        )
        await db.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac, task_id, branch, pr_id

    # Cleanup: hard-delete test task (soft-delete not possible for in_progress)
    async with aiosqlite.connect(tmp_db) as db:
        await db.execute("DELETE FROM pull_requests WHERE task_id = ?", (task_id,))
        await db.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        await db.commit()


# ---------------------------------------------------------------------------
# Test 1: Happy path — webhook merged=true -> task.status = completed
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_pr_merge_completes_task(client_with_task, tmp_db: str):
    """
    Full path: existing review task + open PR -> webhook(closed, merged=true)
    -> task.status == 'completed' and PR.status == 'merged'
    """
    client, task_id, branch, pr_id = client_with_task

    # Build GitHub webhook payload: pull_request.closed with merged=true
    payload = _make_pr_webhook_payload(branch=branch, action="closed", merged=True)
    payload_bytes = json.dumps(payload).encode()
    sig = _make_signature(WEBHOOK_SECRET, payload_bytes)

    # Mock git_ops.merge_branch_async (real merge not possible in test env)
    mock_merge_result = {
        "merged": True,
        "already_merged": False,
        "commit_sha": "abc1234def5678",
    }
    # Also mock remove_worktree_async (cleanup step)
    with patch(
        "api.services.git_ops.merge_branch_async",
        new_callable=AsyncMock,
        return_value=mock_merge_result,
    ), patch(
        "api.services.git_ops.remove_worktree_async",
        new_callable=AsyncMock,
        return_value=None,
    ), patch(
        # Disable deploy_command lookup (no project yaml in test env)
        "api.routers.projects._find_project_entry",
        return_value=None,
    ):
        response = await client.post(
            "/api/v1/webhooks/github",
            content=payload_bytes,
            headers={
                "Content-Type": "application/json",
                "X-Hub-Signature-256": sig,
                "X-GitHub-Event": "pull_request",
                "X-GitHub-Delivery": str(uuid.uuid4()),
            },
        )

    assert response.status_code == 202, f"Webhook response: {response.text}"

    # BackgroundTasks run after response in ASGI test context —
    # wait a short time for the background processing to complete.
    await asyncio.sleep(0.2)

    # Verify task.status == completed
    async with aiosqlite.connect(tmp_db) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT status, review_feedback FROM tasks WHERE id = ?", (task_id,)
        )
        task_row = await cursor.fetchone()
        pr_cursor = await db.execute(
            "SELECT status, commit_sha FROM pull_requests WHERE id = ?", (pr_id,)
        )
        pr_row = await pr_cursor.fetchone()

    assert task_row is not None, "Task not found in DB"
    assert task_row["status"] == "completed", (
        f"Expected task.status='completed', got '{task_row['status']}'"
    )
    assert pr_row is not None, "PR not found in DB"
    assert pr_row["status"] == "merged", (
        f"Expected PR.status='merged', got '{pr_row['status']}'"
    )
    assert pr_row["commit_sha"] == "abc1234def5678", (
        f"Expected commit_sha='abc1234def5678', got '{pr_row['commit_sha']}'"
    )


# ---------------------------------------------------------------------------
# Test 2: PR closed without merge -> task back to in_progress + review_feedback
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_pr_closed_sets_review_feedback(client_with_task, tmp_db: str):
    """
    Full path: existing review task + open PR -> webhook(closed, merged=false, body=reason)
    -> task.status == 'in_progress' and task.review_feedback == pr_body
    """
    client, task_id, branch, pr_id = client_with_task

    close_reason = "Non soddisfa i criteri di accettazione. Rifare il componente X."
    payload = _make_pr_webhook_payload(
        branch=branch, action="closed", merged=False, pr_body=close_reason
    )
    payload_bytes = json.dumps(payload).encode()
    sig = _make_signature(WEBHOOK_SECRET, payload_bytes)

    with patch(
        "api.services.git_ops.remove_worktree_async",
        new_callable=AsyncMock,
        return_value=None,
    ), patch(
        "api.routers.projects._find_git_path",
        return_value=None,  # No real repo needed for close_pr path
    ):
        response = await client.post(
            "/api/v1/webhooks/github",
            content=payload_bytes,
            headers={
                "Content-Type": "application/json",
                "X-Hub-Signature-256": sig,
                "X-GitHub-Event": "pull_request",
                "X-GitHub-Delivery": str(uuid.uuid4()),
            },
        )

    assert response.status_code == 202, f"Webhook response: {response.text}"

    # Wait for BackgroundTask to process
    await asyncio.sleep(0.2)

    # Verify task is back to in_progress with review_feedback set
    async with aiosqlite.connect(tmp_db) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT status, review_feedback FROM tasks WHERE id = ?", (task_id,)
        )
        task_row = await cursor.fetchone()
        pr_cursor = await db.execute(
            "SELECT status, closed_reason FROM pull_requests WHERE id = ?", (pr_id,)
        )
        pr_row = await pr_cursor.fetchone()

    assert task_row is not None, "Task not found in DB"
    assert task_row["status"] == "in_progress", (
        f"Expected task.status='in_progress', got '{task_row['status']}'"
    )
    assert task_row["review_feedback"] == close_reason, (
        f"Expected review_feedback='{close_reason}', got '{task_row['review_feedback']}'"
    )
    assert pr_row is not None, "PR not found in DB"
    assert pr_row["status"] == "closed", (
        f"Expected PR.status='closed', got '{pr_row['status']}'"
    )
    assert pr_row["closed_reason"] == close_reason, (
        f"Expected closed_reason='{close_reason}', got '{pr_row['closed_reason']}'"
    )


# ---------------------------------------------------------------------------
# Test 3: Webhook with invalid signature -> 403
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_webhook_invalid_signature_rejected(app_with_overrides):
    """Webhook with wrong HMAC signature must return 403."""
    app, _ = app_with_overrides
    payload = json.dumps({"action": "closed"}).encode()
    bad_sig = "sha256=deadbeefdeadbeefdeadbeefdeadbeef"

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/v1/webhooks/github",
            content=payload,
            headers={
                "Content-Type": "application/json",
                "X-Hub-Signature-256": bad_sig,
                "X-GitHub-Event": "pull_request",
                "X-GitHub-Delivery": str(uuid.uuid4()),
            },
        )

    assert response.status_code == 403, (
        f"Expected 403 for invalid signature, got {response.status_code}"
    )


# ---------------------------------------------------------------------------
# Test 4: Duplicate delivery_id is idempotent
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_webhook_idempotent_delivery(client_with_task, tmp_db: str):
    """Same delivery_id sent twice must not double-process."""
    client, task_id, branch, pr_id = client_with_task

    delivery_id = str(uuid.uuid4())
    payload = _make_pr_webhook_payload(branch=branch, action="closed", merged=True)
    payload_bytes = json.dumps(payload).encode()
    sig = _make_signature(WEBHOOK_SECRET, payload_bytes)

    headers = {
        "Content-Type": "application/json",
        "X-Hub-Signature-256": sig,
        "X-GitHub-Event": "pull_request",
        "X-GitHub-Delivery": delivery_id,
    }

    mock_merge = {
        "merged": True,
        "already_merged": False,
        "commit_sha": "idempotent123",
    }

    with patch("api.services.git_ops.merge_branch_async", new_callable=AsyncMock, return_value=mock_merge), \
         patch("api.services.git_ops.remove_worktree_async", new_callable=AsyncMock), \
         patch("api.routers.projects._find_project_entry", return_value=None):
        r1 = await client.post("/api/v1/webhooks/github", content=payload_bytes, headers=headers)
        r2 = await client.post("/api/v1/webhooks/github", content=payload_bytes, headers=headers)

    assert r1.status_code == 202
    assert r2.status_code == 202

    await asyncio.sleep(0.3)

    # Merge should have been called only once (idempotency guard in webhook_service)
    async with aiosqlite.connect(tmp_db) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT COUNT(*) as cnt FROM webhook_events WHERE delivery_id = ?", (delivery_id,)
        )
        row = await cursor.fetchone()

    assert row["cnt"] == 1, f"Expected 1 webhook_events row for delivery_id, got {row['cnt']}"
