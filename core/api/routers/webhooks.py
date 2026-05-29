# v1.5.0 - 2026-05-16 - KG PR-Impact sub-01 D3: /webhooks/git/pr-pushed endpoint
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
from datetime import datetime, timezone
from typing import Annotated, Literal

import aiosqlite
from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field, ValidationError

from core.api.config import settings
from core.api.db import get_db, get_write_db
from core.api.services.ci_service import handle_github_ci_event, verify_github_signature
from core.api.services.pr_impact_pipeline.dispatcher import dispatch_job, enqueue_job
from core.api.services.webhook_service import process_webhook_event

logger = logging.getLogger(__name__)

router = APIRouter(tags=["webhooks"])

_REPLAY_WINDOW_SECONDS = 300
_PR_TASK_ID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)


def verify_signature(raw_body: bytes, sig_header: str) -> None:
    """Timing-safe HMAC-SHA256 verification. Raises HTTPException(403) on failure.

    Dev mode: if github_webhook_secret is empty, all requests are accepted without
    signature check. This allows local testing without configuring a secret.
    """
    if not settings.github_webhook_secret:
        if settings.is_production:
            raise HTTPException(status_code=500, detail="Webhook secret not configured")
        logger.debug("GitHub webhook: secret not configured, skipping signature verification (dev mode)")
        return
    if not sig_header or not sig_header.startswith("sha256="):
        raise HTTPException(status_code=403, detail="Missing or invalid signature header")
    expected = hmac.new(
        settings.github_webhook_secret.encode(),
        raw_body,
        hashlib.sha256,
    ).hexdigest()
    received = sig_header[len("sha256="):]
    if not hmac.compare_digest(expected, received):
        raise HTTPException(status_code=403, detail="Signature mismatch")


@router.post("/api/v1/webhooks/github", status_code=202)
async def github_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
) -> dict:
    """Receive GitHub webhook events. Responds 202 immediately; processing runs in background."""
    # CRITICAL: read raw bytes BEFORE any JSON parsing (stream is consumed on first read)
    raw_body = await request.body()

    sig_header = request.headers.get("X-Hub-Signature-256", "")
    verify_signature(raw_body, sig_header)

    event = request.headers.get("X-GitHub-Event", "")
    delivery_id = request.headers.get("X-GitHub-Delivery", "")

    try:
        payload = json.loads(raw_body)
    except json.JSONDecodeError as exc:
        logger.warning("GitHub webhook: invalid JSON body (delivery=%s): %s", delivery_id, exc)
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    logger.info(
        "GitHub webhook received: event=%s delivery=%s action=%s",
        event, delivery_id, payload.get("action"),
    )

    background_tasks.add_task(
        process_webhook_event,
        event=event,
        delivery_id=delivery_id,
        payload=payload,
    )

    return {"accepted": True}


@router.post("/api/v1/webhooks/ci", status_code=202)
async def ci_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    db: aiosqlite.Connection = Depends(get_write_db),
) -> dict:
    """Receive GitHub CI webhook events (check_run, workflow_run).

    Verifies HMAC-SHA256 signature, then processes asynchronously.
    Responds 202 immediately; CI tracking runs in background.
    """
    raw_body = await request.body()

    sig_header = request.headers.get("X-Hub-Signature-256", "")
    # Reuse existing verify_signature for HMAC check (same secret)
    verify_signature(raw_body, sig_header)

    event = request.headers.get("X-GitHub-Event", "")
    delivery_id = request.headers.get("X-GitHub-Delivery", "")

    # Only process CI-relevant events
    if event not in ("check_run", "workflow_run"):
        return {"accepted": True, "skipped": True, "reason": f"event {event} not CI-relevant"}

    try:
        payload = json.loads(raw_body)
    except json.JSONDecodeError as exc:
        logger.warning("CI webhook: invalid JSON body (delivery=%s): %s", delivery_id, exc)
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    logger.info(
        "CI webhook received: event=%s delivery=%s action=%s",
        event, delivery_id, payload.get("action"),
    )

    # Process synchronously (fast DB insert, not heavy I/O)
    result = await handle_github_ci_event(payload, delivery_id, db)

    return {"accepted": True, **result}


# ---------------------------------------------------------------------------
# KG PR-Impact sub-01 D3: /webhooks/git/pr-pushed
# ---------------------------------------------------------------------------


_CommitShaStr = Annotated[str, Field(pattern=r"^[0-9a-f]{7,40}$")]


class PRHead(BaseModel):
    ref: Annotated[str, Field(min_length=1, max_length=255)]
    sha: _CommitShaStr


class PRPayload(BaseModel):
    id: int | str  # GitHub uses int; Gitea synthetic deliveries may send str
    number: int
    head: PRHead
    base: PRHead


class GitHubPRPushedPayload(BaseModel):
    action: Literal["opened", "synchronize", "reopened", "closed"]
    pull_request: PRPayload
    repository: dict
    # `pr_task_id` is OUR canonical PR identifier (pull_requests.task_id),
    # passed through by the GitHub Actions glue. Required so the populator
    # can attribute the touch to the correct PR row even when GitHub's own
    # `pull_request.id` is opaque.
    pr_task_id: Annotated[str, Field(min_length=36, max_length=36)]


def _derive_pr_node_id(payload: GitHubPRPushedPayload) -> str:
    """Synthesize `pr:artifact:<uuid>` from the explicit `pr_task_id` field."""
    task_id = payload.pr_task_id
    if not _PR_TASK_ID_RE.match(task_id):
        raise HTTPException(status_code=400, detail="invalid pr_task_id (expected uuid)")
    return f"pr:artifact:{task_id}"


async def _mark_delivery_failed(
    db: aiosqlite.Connection,
    delivery_id: str,
    error: str,
) -> None:
    await db.execute(
        """
        UPDATE webhook_deliveries
           SET status='failed', error_summary=?,
               processed_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')
         WHERE delivery_id=?
        """,
        (error[:500], delivery_id),
    )
    await db.commit()


@router.post("/api/v1/webhooks/git/pr-pushed", status_code=202)
async def pr_pushed_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    delivery_id: str = Header(alias="X-GitHub-Delivery"),
    signature: str = Header(default="", alias="X-Hub-Signature-256"),
    delivery_ts: str | None = Header(default=None, alias="X-GitHub-Delivery-Timestamp"),
    db: aiosqlite.Connection = Depends(get_write_db),
) -> dict:
    """Receive a PR-push event and queue a populator job.

    Pipeline (per plan §D3):
      1. Read raw bytes BEFORE JSON parse (HMAC must hash exact request body)
      2. Verify HMAC via the existing helper
      3. Replay window check (X-GitHub-Delivery-Timestamp within 5 min)
      4. Idempotent INSERT into webhook_deliveries (delivery_id PK)
      5. Pydantic schema validation
      6. Action whitelist (opened / synchronize / reopened / closed only)
      7. Enqueue + dispatch via BackgroundTasks
    """
    raw_body = await request.body()
    verify_signature(raw_body, signature)

    # Replay window guard — protects against rerunning an old delivery on a
    # stolen secret. Skipped when the header is missing (GitHub sometimes
    # omits it on synthetic deliveries from Actions).
    if delivery_ts:
        try:
            sent_at = datetime.fromisoformat(delivery_ts.replace("Z", "+00:00"))
        except ValueError:
            raise HTTPException(status_code=400, detail="malformed delivery timestamp")
        if sent_at.tzinfo is None:
            sent_at = sent_at.replace(tzinfo=timezone.utc)
        skew = abs((datetime.now(timezone.utc) - sent_at).total_seconds())
        if skew > _REPLAY_WINDOW_SECONDS:
            raise HTTPException(status_code=401, detail="delivery outside replay window")

    payload_sha = hashlib.sha256(raw_body).hexdigest()

    # Idempotency: PK conflict on duplicate delivery → return early with 200.
    cursor = await db.execute(
        """
        INSERT OR IGNORE INTO webhook_deliveries (
            delivery_id, source, event_type, payload_sha256, status
        ) VALUES (?, 'github', 'pull_request', ?, 'pending')
        """,
        (delivery_id, payload_sha),
    )
    await db.commit()
    if cursor.rowcount == 0:
        logger.info("pr_pushed_webhook duplicate delivery_id=%s", delivery_id)
        return {"status": "duplicate", "delivery_id": delivery_id}

    # Pydantic validation (after idempotency insert so duplicates exit cheap).
    try:
        payload = GitHubPRPushedPayload.model_validate_json(raw_body)
    except ValidationError as exc:
        await _mark_delivery_failed(db, delivery_id, f"schema: {str(exc)[:300]}")
        # Don't leak validation details to the caller.
        raise HTTPException(status_code=400, detail="invalid payload schema") from exc

    # Action whitelist. `closed` proceeds because the populator distinguishes
    # merged vs abandoned via the head commit ancestry — D4 sweep handles it.
    if payload.action not in ("opened", "synchronize", "reopened", "closed"):
        await db.execute(
            """
            UPDATE webhook_deliveries
               SET status='skipped',
                   processed_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')
             WHERE delivery_id=?
            """,
            (delivery_id,),
        )
        await db.commit()
        return {"status": "ignored", "action": payload.action}

    pr_node_id = _derive_pr_node_id(payload)
    project_id = payload.repository.get("name")

    # Link the webhook_deliveries row to the PR for forensic queries.
    await db.execute(
        """
        UPDATE webhook_deliveries
           SET pr_id=?, status='processed',
               processed_at=strftime('%Y-%m-%dT%H:%M:%fZ','now'),
               project_id=?
         WHERE delivery_id=?
        """,
        (payload.pr_task_id, project_id, delivery_id),
    )
    await db.commit()

    # Honor the shadow gate — `off` means we record the delivery + ack but
    # never enqueue. Useful for replay testing without touching the queue.
    pr_impact_enabled = getattr(settings, "pr_impact_enabled", "shadow")
    if pr_impact_enabled == "off":
        return {
            "status": "recorded",
            "delivery_id": delivery_id,
            "pr_task_id": payload.pr_task_id,
            "skipped_reason": "pr_impact_enabled=off",
        }

    try:
        job_id = await enqueue_job(
            db,
            pr_id=payload.pr_task_id,
            delivery_id=delivery_id,
            payload={"pr_id": payload.pr_task_id, "action": payload.action},
            project_id=project_id,
        )
    except ValueError as exc:
        # The PR exists on GitHub but hasn't been registered in MarvisX yet —
        # mark the delivery `skipped` and ACK so GitHub doesn't retry.
        await db.execute(
            """
            UPDATE webhook_deliveries
               SET status='skipped', error_summary=?,
                   processed_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')
             WHERE delivery_id=?
            """,
            (f"unknown PR: {exc}", delivery_id),
        )
        await db.commit()
        return {
            "status": "skipped",
            "delivery_id": delivery_id,
            "reason": "pr_task_id not registered in pull_requests",
        }

    # Resolve the DB path from settings so the BackgroundTask doesn't need
    # to crack open Depends inside its own coroutine.
    db_path = getattr(settings, "db_path", "/data/pir/console.db") or "/data/pir/console.db"
    background_tasks.add_task(dispatch_job, job_id, db_path=db_path)

    return {
        "status": "queued",
        "delivery_id": delivery_id,
        "pr_node_id": pr_node_id,
        "pr_task_id": payload.pr_task_id,
        "job_id": job_id,
        "shadow_mode": pr_impact_enabled == "shadow",
    }
