# v1.0.0 - 2026-03-13 - CI check status endpoints for Console + MCP tools
from __future__ import annotations

import logging

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException, Query

from core.api.db import get_db, get_write_db
from core.api.models import UserInfo
from core.api.security import require_any_auth

logger = logging.getLogger(__name__)

router = APIRouter(tags=["ci-checks"])


@router.get("/api/v1/ci-checks")
async def list_ci_checks(
    task_id: str | None = Query(None, description="Filter by task ID"),
    project: str | None = Query(None, description="Filter by project (via task)"),
    status: str | None = Query(None, description="Filter by status"),
    limit: int = Query(50, ge=1, le=200),
    user: UserInfo = Depends(require_any_auth),
    db: aiosqlite.Connection = Depends(get_db),
) -> list[dict]:
    """List CI checks, optionally filtered by task, project, or status."""
    conditions = ["1=1"]
    params: list = []

    # Workspace isolation
    ws_id = user.workspace_id or "ws_default"
    conditions.append("COALESCE(c.workspace_id, 'ws_default') = ?")
    params.append(ws_id)

    if task_id:
        conditions.append("c.task_id = ?")
        params.append(task_id)

    if project:
        conditions.append("c.task_id IN (SELECT id FROM tasks WHERE project = ?)")
        params.append(project)

    if status:
        conditions.append("c.status = ?")
        params.append(status)

    params.append(limit)

    query = f"""
        SELECT c.id, c.task_id, c.check_name, c.status, c.details_url,
               c.output_summary, c.started_at, c.completed_at, c.attempt,
               c.delivery_id, c.created_at
        FROM ci_checks c
        WHERE {' AND '.join(conditions)}
        ORDER BY c.created_at DESC
        LIMIT ?
    """

    async with db.execute(query, params) as cursor:
        rows = await cursor.fetchall()

    return [dict(row) for row in rows]


@router.get("/api/v1/ci-checks/summary")
async def ci_checks_summary(
    task_id: str = Query(..., description="Task ID to summarize CI status for"),
    user: UserInfo = Depends(require_any_auth),
    db: aiosqlite.Connection = Depends(get_db),
) -> dict:
    """Get CI summary for a task: counts by status + required check gate."""
    ws_id = user.workspace_id or "ws_default"

    # Counts by status
    async with db.execute(
        """SELECT status, COUNT(*) as cnt FROM ci_checks
           WHERE task_id = ? AND COALESCE(workspace_id, 'ws_default') = ?
           GROUP BY status""",
        (task_id, ws_id),
    ) as cursor:
        rows = await cursor.fetchall()

    counts = {row["status"]: row["cnt"] for row in rows}

    # Check required CI passes (merge gate)
    from core.api.services.ci_service import check_required_ci_passes

    # Get task project
    async with db.execute("SELECT project FROM tasks WHERE id = ?", (task_id,)) as cursor:
        task_row = await cursor.fetchone()

    failing_checks: list[str] = []
    if task_row:
        failing_checks = await check_required_ci_passes(task_id, task_row["project"], db)

    return {
        "task_id": task_id,
        "counts": counts,
        "total": sum(counts.values()),
        "all_passed": counts.get("failure", 0) == 0 and counts.get("error", 0) == 0,
        "required_failing": failing_checks,
        "merge_blocked": len(failing_checks) > 0,
    }


@router.post("/api/v1/ci-checks/{check_id}/retry")
async def retry_ci_check(
    check_id: str,
    user: UserInfo = Depends(require_any_auth),
    db: aiosqlite.Connection = Depends(get_write_db),
) -> dict:
    """Mark a CI check for retry (increments attempt counter, resets status to pending).

    The actual re-run must be triggered externally (e.g., GitHub Actions re-run).
    This just resets the tracking state so the next webhook delivery updates correctly.
    """
    ws_id = user.workspace_id or "ws_default"

    async with db.execute(
        "SELECT * FROM ci_checks WHERE id = ? AND COALESCE(workspace_id, 'ws_default') = ?",
        (check_id, ws_id),
    ) as cursor:
        row = await cursor.fetchone()

    if not row:
        raise HTTPException(status_code=404, detail="CI check not found")

    new_attempt = (row["attempt"] or 1) + 1

    await db.execute(
        """UPDATE ci_checks SET status = 'pending', attempt = ?, completed_at = NULL,
           output_summary = NULL WHERE id = ?""",
        (new_attempt, check_id),
    )
    await db.commit()

    logger.info("CI check %s reset for retry (attempt %d)", check_id, new_attempt)

    return {"id": check_id, "status": "pending", "attempt": new_attempt}
