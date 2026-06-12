# v1.0.0 - 2026-03-13 - CI/CD feedback loop: check tracking + merge gate + notifications
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import logging
import uuid
from datetime import datetime, timezone
from urllib.parse import urlparse

import aiosqlite

from core.api.config import settings

logger = logging.getLogger(__name__)

# SSRF protection for details_url: only allow these domains
_ALLOWED_CI_DOMAINS = {"github.com", "api.github.com"}


def verify_github_signature(payload_body: bytes, signature_header: str, secret: str) -> bool:
    """Verify GitHub webhook HMAC-SHA256 signature.

    CRITICAL: Use raw body bytes, NOT re-serialized JSON (would break HMAC).
    """
    if not signature_header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(
        secret.encode(), payload_body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature_header)


def _validate_details_url(url: str | None) -> str | None:
    """SSRF protection: only allow HTTPS URLs from known CI domains."""
    if not url:
        return None
    try:
        parsed = urlparse(url)
        if parsed.scheme != "https":
            return None
        if parsed.hostname not in _ALLOWED_CI_DOMAINS:
            return None
        return url
    except Exception:
        return None


async def handle_github_ci_event(
    payload: dict,
    delivery_id: str,
    db: aiosqlite.Connection,
) -> dict:
    """Process GitHub check_run or workflow_run webhook event.

    Returns summary of what was processed.
    """
    # Dedup by delivery_id
    if delivery_id:
        async with db.execute(
            "SELECT id FROM ci_checks WHERE delivery_id = ?", (delivery_id,)
        ) as cursor:
            if await cursor.fetchone():
                return {"status": "duplicate", "delivery_id": delivery_id}

    # Handle check_run events (most common for GitHub Actions)
    check_run = payload.get("check_run")
    if check_run:
        return await _process_check_run(check_run, delivery_id, db)

    # Handle workflow_run events
    workflow_run = payload.get("workflow_run")
    if workflow_run:
        return await _process_workflow_run(workflow_run, delivery_id, db)

    return {"status": "ignored", "reason": "no check_run or workflow_run in payload"}


async def _process_check_run(
    check_run: dict,
    delivery_id: str,
    db: aiosqlite.Connection,
) -> dict:
    """Process a GitHub check_run event."""
    check_name = check_run.get("name", "unknown")
    conclusion = check_run.get("conclusion")  # success, failure, neutral, cancelled, etc.
    status_gh = check_run.get("status")  # queued, in_progress, completed

    # Map GitHub status to our status
    if status_gh == "completed":
        status = "success" if conclusion == "success" else "failure"
    elif status_gh == "in_progress":
        status = "running"
    else:
        status = "pending"

    details_url = _validate_details_url(check_run.get("details_url"))
    output_summary = None
    if check_run.get("output", {}).get("summary"):
        output_summary = check_run["output"]["summary"][:2000]  # truncate

    # Find task_id from PR — check_run.pull_requests[].head.ref contains branch name
    task_id = None
    for pr in check_run.get("pull_requests", []):
        branch = pr.get("head", {}).get("ref", "")
        # Extract task ID from branch name feat/task-{uuid}
        if "task-" in branch:
            task_id = branch.split("task-")[-1][:36]  # UUID length
            break

    if not task_id:
        return {"status": "ignored", "reason": "no task_id found in PR branches"}

    check_id = uuid.uuid4().hex[:32]
    now = datetime.now(timezone.utc).isoformat()

    await db.execute(
        """INSERT INTO ci_checks (id, task_id, check_name, status, details_url, output_summary,
           started_at, completed_at, delivery_id, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(task_id, check_name, attempt)
           DO UPDATE SET status = excluded.status, completed_at = excluded.completed_at,
                         output_summary = excluded.output_summary, details_url = excluded.details_url""",
        (
            check_id, task_id, check_name, status, details_url, output_summary,
            check_run.get("started_at"), check_run.get("completed_at") if status_gh == "completed" else None,
            delivery_id, now,
        ),
    )
    await db.commit()

    # Notify on failure
    if status == "failure":
        await _notify_ci_failure(task_id, check_name, 1, db)

    return {"status": "processed", "task_id": task_id, "check_name": check_name, "result": status}


async def _process_workflow_run(
    workflow_run: dict,
    delivery_id: str,
    db: aiosqlite.Connection,
) -> dict:
    """Process a GitHub workflow_run event."""
    check_name = workflow_run.get("name", "unknown")
    conclusion = workflow_run.get("conclusion")
    status_gh = workflow_run.get("status")

    if status_gh == "completed":
        status = "success" if conclusion == "success" else "failure"
    elif status_gh == "in_progress":
        status = "running"
    else:
        status = "pending"

    # Extract task_id from head_branch
    head_branch = workflow_run.get("head_branch", "")
    task_id = None
    if "task-" in head_branch:
        task_id = head_branch.split("task-")[-1][:36]

    if not task_id:
        return {"status": "ignored", "reason": "no task_id in head_branch"}

    details_url = _validate_details_url(workflow_run.get("html_url"))
    check_id = uuid.uuid4().hex[:32]
    now = datetime.now(timezone.utc).isoformat()

    await db.execute(
        """INSERT INTO ci_checks (id, task_id, check_name, status, details_url,
           started_at, completed_at, delivery_id, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(task_id, check_name, attempt)
           DO UPDATE SET status = excluded.status, completed_at = excluded.completed_at""",
        (
            check_id, task_id, check_name, status, details_url,
            workflow_run.get("run_started_at"), workflow_run.get("updated_at") if status_gh == "completed" else None,
            delivery_id, now,
        ),
    )
    await db.commit()

    if status == "failure":
        await _notify_ci_failure(task_id, check_name, 1, db)

    return {"status": "processed", "task_id": task_id, "check_name": check_name, "result": status}


async def _notify_ci_failure(
    task_id: str,
    check_name: str,
    attempt: int,
    db: aiosqlite.Connection,
) -> None:
    """Create notification on CI failure. Auto-retry deferred to v2."""
    try:
        # Find task owner
        async with db.execute(
            "SELECT owner_id, workspace_id FROM tasks WHERE id = ?", (task_id,)
        ) as cursor:
            task = await cursor.fetchone()

        if not task or not task["owner_id"]:
            return

        from core.api.services.events import emit_event
        await emit_event(
            db,
            event_type="ci.failure",
            project=None,
            actor_id=None,
            target_type="task",
            target_id=task_id,
            payload={"check_name": check_name, "attempt": attempt},
            workspace_id=task["workspace_id"],
        )
    except Exception:
        logger.warning("Failed to notify CI failure for task %s", task_id, exc_info=True)


async def check_required_ci_passes(
    task_id: str,
    project: str | None,
    db: aiosqlite.Connection,
) -> list[str]:
    """Return list of required checks that haven't passed. Empty = all clear.

    Used by pr_service merge gate.
    """
    if not project:
        return []

    # Get required checks for project
    async with db.execute(
        "SELECT required_checks FROM project_ci_config WHERE project = ?", (project,)
    ) as cursor:
        config_row = await cursor.fetchone()

    if not config_row or not config_row["required_checks"]:
        return []  # No required checks configured

    try:
        required = json.loads(config_row["required_checks"])
    except (json.JSONDecodeError, TypeError):
        return []

    if not required:
        return []

    # Check which required checks haven't passed
    placeholders = ",".join("?" for _ in required)
    async with db.execute(
        f"""SELECT check_name FROM ci_checks
            WHERE task_id = ? AND check_name IN ({placeholders}) AND status = 'success'
            GROUP BY check_name""",
        [task_id, *required],
    ) as cursor:
        passed_rows = await cursor.fetchall()

    passed = {r["check_name"] for r in passed_rows}
    return [c for c in required if c not in passed]
