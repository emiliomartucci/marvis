# v1.0.0 - 2026-05-16 - KG PR-Impact sub-01 D5: admin endpoints for ops + agent parity
"""Admin endpoints for the PR-impact pipeline.

Three operations (plan §D5):

- POST /admin/pr-impact/backfill   — enqueue a populator job for a given PR
                                      (used by the agent MCP tool when
                                      replaying after webhook hiccups)
- GET  /admin/pr-impact/deliveries — list recent webhook deliveries with
                                      their pipeline state, joined with
                                      job rows + last error for ops triage
- POST /admin/pr-impact/dlq/replay — promote a dead-letter delivery back
                                      to `pending`, reset job attempts,
                                      and re-enqueue

All endpoints require `operator|admin|super_admin`. The pattern mirrors
other admin-only routers for naming and RBAC consistency.
"""
from __future__ import annotations

import logging
from typing import Any, Literal

import aiosqlite
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from core.api.config import settings
from core.api.db import get_db, get_write_db
from core.api.rbac import require_role
from core.api.services.pr_impact_pipeline.dispatcher import dispatch_job, enqueue_job

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/admin/pr-impact", tags=["admin", "pr-impact"])


class BackfillRequest(BaseModel):
    pr_task_id: str = Field(min_length=36, max_length=36)
    force: bool = False  # reserved for D2v2 incremental gate
    incremental: bool = True


class BackfillResponse(BaseModel):
    job_id: str
    status: Literal["queued", "skipped"]
    reason: str | None = None


class DeliverySummary(BaseModel):
    delivery_id: str
    source: str
    event_type: str
    pr_id: str | None
    status: str
    received_at: str
    processed_at: str | None
    retry_count: int
    error_summary: str | None
    job_count: int


class DeliveriesResponse(BaseModel):
    items: list[DeliverySummary]
    total: int


class DlqReplayRequest(BaseModel):
    delivery_id: str = Field(min_length=1, max_length=128)


class DlqReplayResponse(BaseModel):
    delivery_id: str
    job_id: str
    reset_attempts: bool


def _db_path() -> str:
    return getattr(settings, "db_path", "/data/pir/console.db") or "/data/pir/console.db"


@router.post("/backfill", response_model=BackfillResponse, status_code=202)
async def admin_backfill(
    body: BackfillRequest,
    background_tasks: BackgroundTasks,
    db: aiosqlite.Connection = Depends(get_write_db),
    _=Depends(require_role("operator", "admin", "super_admin")),
) -> BackfillResponse:
    """Enqueue a populator job for an arbitrary PR.

    Same code path as the webhook handler (single dispatcher entry point).
    Honors `PR_IMPACT_ENABLED='off'` by returning `skipped` without queuing.
    """
    pr_impact_enabled = getattr(settings, "pr_impact_enabled", "shadow")
    if pr_impact_enabled == "off":
        return BackfillResponse(
            job_id="",
            status="skipped",
            reason="pr_impact_enabled=off",
        )

    try:
        job_id = await enqueue_job(
            db,
            pr_id=body.pr_task_id,
            delivery_id=None,  # backfill jobs aren't tied to a webhook delivery
            payload={
                "pr_id": body.pr_task_id,
                "source": "admin_backfill",
                "force": body.force,
                "incremental": body.incremental,
            },
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    background_tasks.add_task(dispatch_job, job_id, db_path=_db_path())
    return BackfillResponse(job_id=job_id, status="queued")


@router.get("/deliveries", response_model=DeliveriesResponse)
async def admin_list_deliveries(
    pr_id: str | None = Query(default=None, max_length=128),
    status: str | None = Query(default=None, max_length=32),
    limit: int = Query(default=50, ge=1, le=200),
    db: aiosqlite.Connection = Depends(get_db),
    _=Depends(require_role("operator", "admin", "super_admin")),
) -> DeliveriesResponse:
    """List recent webhook deliveries with their job count + last error."""
    conditions: list[str] = []
    params: list[Any] = []
    if pr_id:
        conditions.append("wd.pr_id = ?")
        params.append(pr_id)
    if status:
        conditions.append("wd.status = ?")
        params.append(status)
    where_sql = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    sql = f"""
        SELECT
            wd.delivery_id, wd.source, wd.event_type, wd.pr_id,
            wd.status, wd.received_at, wd.processed_at, wd.retry_count,
            wd.error_summary,
            (SELECT COUNT(*) FROM pr_impact_jobs j WHERE j.delivery_id = wd.delivery_id) AS job_count
          FROM webhook_deliveries wd
         {where_sql}
         ORDER BY wd.received_at DESC
         LIMIT ?
    """
    params.append(limit)
    rows = await (await db.execute(sql, params)).fetchall()
    items = [
        DeliverySummary(
            delivery_id=r[0],
            source=r[1],
            event_type=r[2],
            pr_id=r[3],
            status=r[4],
            received_at=r[5],
            processed_at=r[6],
            retry_count=r[7],
            error_summary=r[8],
            job_count=r[9],
        )
        for r in rows
    ]
    return DeliveriesResponse(items=items, total=len(items))


@router.post("/dlq/replay", response_model=DlqReplayResponse, status_code=202)
async def admin_replay_dlq(
    body: DlqReplayRequest,
    background_tasks: BackgroundTasks,
    db: aiosqlite.Connection = Depends(get_write_db),
    _=Depends(require_role("admin", "super_admin")),  # tighter than backfill — destructive reset
) -> DlqReplayResponse:
    """Re-enqueue a dead-letter delivery: reset job attempts + status='queued'."""
    row = await (
        await db.execute(
            "SELECT pr_id FROM webhook_deliveries WHERE delivery_id=?",
            (body.delivery_id,),
        )
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="delivery not found")
    if row[0] is None:
        raise HTTPException(status_code=400, detail="delivery has no pr_id; cannot replay")

    pr_task_id = row[0]

    # Reset the delivery row to `pending` so the next sweep sees it.
    await db.execute(
        """
        UPDATE webhook_deliveries
           SET status='pending', retry_count=retry_count+1,
               error_summary=NULL,
               processed_at=NULL
         WHERE delivery_id=?
        """,
        (body.delivery_id,),
    )

    # Reset any existing job rows tied to this delivery, then enqueue a fresh one.
    await db.execute(
        """
        UPDATE pr_impact_jobs
           SET status='queued', attempts=0, last_error=NULL,
               started_at=NULL, finished_at=NULL,
               claim_lease_until=NULL
         WHERE delivery_id=?
        """,
        (body.delivery_id,),
    )
    await db.commit()

    try:
        new_job_id = await enqueue_job(
            db,
            pr_id=pr_task_id,
            delivery_id=body.delivery_id,
            payload={"pr_id": pr_task_id, "source": "dlq_replay"},
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    background_tasks.add_task(dispatch_job, new_job_id, db_path=_db_path())
    return DlqReplayResponse(
        delivery_id=body.delivery_id,
        job_id=new_job_id,
        reset_attempts=True,
    )
