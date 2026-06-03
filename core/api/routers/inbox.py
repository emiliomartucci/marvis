# v1.1.0 - 2026-04-14 - Single-writer: reclassify_inbox_items uses get_write_db (batch 5/6)
from __future__ import annotations

import logging
import time
from collections import defaultdict
from typing import Literal

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from core.api.db import get_db, get_write_db, write_db
from core.api.models import (
    InboxGmailSyncCandidate,
    InboxGmailSyncCompleteRequest,
    InboxIngestBatchRequest,
    InboxIngestBatchResponse,
    InboxIngestRequest,
    InboxIngestResponse,
    InboxItemDetail,
    InboxItemSummary,
    InboxStatsResponse,
    InboxStatusUpdateRequest,
    InboxTaxonomyUpdateRequest,
    InboxTaxonomyUpdateResponse,
    InboxTriageDecisionRequest,
    InboxTriageDecisionResponse,
    UserInfo,
)
from core.api.rbac import require_role
from core.api.security import get_current_user_or_agent, require_agent_token_scope
from core.api.config import settings
from core.api.services.inbox import ingest_item, ingest_items_batch
from core.api.services.inbox_llm_classifier import schedule_classification
from core.api.services.inbox_sources import (
    create_source,
    delete_source,
    get_source,
    get_source_metrics,
    list_sources,
    update_source,
    validate_public_url,
)
from core.api.services.inbox_digest import get_digest_stats, list_digest_items
from core.api.services.inbox_digest_jobs import recompute_digest_now
from core.api.services.inbox_gmail_sync import (
    list_gmail_sync_candidates,
    mark_gmail_sync_complete,
)
from core.api.services.inbox_tldr import get_or_generate_deep_research, get_or_generate_tldr
from core.api.services.inbox_triage import (
    apply_triage_decision,
    get_inbox_item_detail,
    get_inbox_stats,
    get_source_scores,
    get_unread_count,
    list_inbox_items,
    update_inbox_status,
    update_inbox_taxonomy,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/inbox", tags=["inbox"])

# ---------------------------------------------------------------------------
# Rate limit on POST /inbox/ingest (PR B)
# ---------------------------------------------------------------------------
# In-memory sliding window, per-process. 1000 req / 60s per identity — tuned
# for n8n RSS batch bursts (hundreds of articles per poll cycle) while still
# providing an emergency cap against runaway cost attacks. At higher sustained
# rates the LLM cost guard (app_settings daily cap) becomes the binding limit.
# Good enough for a single-worker FastAPI deployment; swap to Redis if we go
# multi-worker.
_INGEST_RATE_LIMITS: dict[str, list[float]] = defaultdict(list)
_INGEST_RATE_LIMITS_MAX_KEYS = 1000


def _check_ingest_rate_limit(
    identity: str,
    *,
    limit: int = 1000,
    window: float = 60.0,
) -> None:
    now = time.time()
    bucket = _INGEST_RATE_LIMITS[identity]
    # Drop expired timestamps in place
    fresh = [t for t in bucket if now - t < window]
    _INGEST_RATE_LIMITS[identity] = fresh
    if len(fresh) >= limit:
        raise HTTPException(status_code=429, detail="Rate limit exceeded")
    fresh.append(now)
    # Evict stale keys opportunistically
    if len(_INGEST_RATE_LIMITS) > _INGEST_RATE_LIMITS_MAX_KEYS:
        stale = [
            k for k, v in _INGEST_RATE_LIMITS.items() if not v or (now - v[-1]) > window
        ]
        for k in stale:
            del _INGEST_RATE_LIMITS[k]


class SourceCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    source_key: str = Field(..., min_length=1, max_length=200)
    feed_url: str | None = Field(None, max_length=500)
    source_type: str = Field("rss", pattern=r"^(rss|email|manual|api|legacy)$")


class SourceUpdateRequest(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=200)
    feed_url: str | None = Field(None, max_length=500)
    active: bool | None = None


@router.post(
    "/ingest", response_model=InboxIngestResponse, status_code=status.HTTP_201_CREATED
)
async def ingest_inbox_item(
    body: InboxIngestRequest,
    user: UserInfo = Depends(require_agent_token_scope("inbox:ingest")),
) -> InboxIngestResponse:
    _check_ingest_rate_limit(f"ingest:{user.username}", limit=1000, window=60)
    async with write_db(label="inbox.ingest.single") as db:
        return await ingest_item(body, user=user, db=db)


@router.post(
    "/ingest/batch",
    response_model=InboxIngestBatchResponse,
    status_code=status.HTTP_200_OK,
)
async def ingest_inbox_items_batch(
    body: InboxIngestBatchRequest,
    user: UserInfo = Depends(require_agent_token_scope("inbox:ingest")),
) -> InboxIngestBatchResponse:
    # Rate limit accounts each item individually so a 500-item batch still
    # respects the 1000/60s identity cap. A single large batch is one lock
    # acquisition + one fsync, so this is intentionally generous.
    for _ in range(len(body.items)):
        _check_ingest_rate_limit(f"ingest:{user.username}", limit=1000, window=60)
    async with write_db(label="inbox.ingest.batch") as db:
        return await ingest_items_batch(body, user=user, db=db)


# ---------------------------------------------------------------------------
# Sources CRUD (PR B)
# ---------------------------------------------------------------------------


@router.get("/sources")
async def list_inbox_sources(
    active_only: bool = Query(False),
    user: UserInfo = Depends(require_role("operator", "admin", "super_admin")),
    db: aiosqlite.Connection = Depends(get_db),
) -> list[dict]:
    return await list_sources(
        db, user.workspace_id or "ws_default", active_only=active_only
    )


@router.get("/sources/{source_id}")
async def get_inbox_source(
    source_id: str,
    user: UserInfo = Depends(require_role("operator", "admin", "super_admin")),
    db: aiosqlite.Connection = Depends(get_db),
) -> dict:
    result = await get_source(db, user.workspace_id or "ws_default", source_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Source not found")
    return result


@router.get("/sources/{source_id}/metrics")
async def get_inbox_source_metrics_endpoint(
    source_id: str,
    range: Literal["24h", "7d", "30d", "total"] = Query("total"),
    user: UserInfo = Depends(require_role("operator", "admin", "super_admin")),
    db: aiosqlite.Connection = Depends(get_db),
) -> dict:
    return await get_source_metrics(
        db, user.workspace_id or "ws_default", source_id, range=range
    )


@router.post("/sources")
async def create_inbox_source(
    body: SourceCreateRequest,
    user: UserInfo = Depends(require_role("admin", "super_admin")),
    db: aiosqlite.Connection = Depends(get_write_db),
) -> dict:
    if body.feed_url:
        validate_public_url(body.feed_url)
    return await create_source(
        db,
        user.workspace_id or "ws_default",
        name=body.name,
        source_key=body.source_key,
        feed_url=body.feed_url,
        source_type=body.source_type,
    )


@router.patch("/sources/{source_id}")
async def update_inbox_source(
    source_id: str,
    body: SourceUpdateRequest,
    user: UserInfo = Depends(require_role("admin", "super_admin")),
    db: aiosqlite.Connection = Depends(get_write_db),
) -> dict:
    if body.feed_url:
        validate_public_url(body.feed_url)
    return await update_source(
        db,
        user.workspace_id or "ws_default",
        source_id,
        name=body.name,
        feed_url=body.feed_url,
        active=body.active,
    )


@router.delete("/sources/{source_id}", status_code=204)
async def delete_inbox_source(
    source_id: str,
    user: UserInfo = Depends(require_role("admin", "super_admin")),
    db: aiosqlite.Connection = Depends(get_write_db),
) -> None:
    await delete_source(db, user.workspace_id or "ws_default", source_id)


@router.get("/items", response_model=list[InboxItemSummary])
async def get_inbox_items(
    needs_triage: bool = Query(
        True, description="Default true: only items with status='unread'"
    ),
    classified: bool | None = Query(None),
    source: str | None = Query(None),
    program: str | None = Query(None),
    topic: str | None = Query(None),
    treatment: str | None = Query(None),
    item_status: str | None = Query(
        None, alias="status", description="Filter by status (unread, read, saved, etc.)"
    ),
    limit: int = Query(20, ge=1, le=200),
    user: UserInfo = Depends(get_current_user_or_agent),
    db: aiosqlite.Connection = Depends(get_db),
) -> list[InboxItemSummary]:
    return await list_inbox_items(
        db,
        user,
        limit=limit,
        needs_triage=needs_triage,
        classified=classified,
        source=source,
        program=program,
        topic=topic,
        treatment=treatment,
        status=item_status,
    )


@router.get("/stats", response_model=InboxStatsResponse)
async def get_inbox_stats_route(
    needs_triage: bool = Query(
        True, description="Default true: only items with status='unread'"
    ),
    classified: bool | None = Query(None),
    source: str | None = Query(None),
    program: str | None = Query(None),
    topic: str | None = Query(None),
    treatment: str | None = Query(None),
    item_status: str | None = Query(
        None, alias="status", description="Filter by status (unread, read, saved, etc.)"
    ),
    user: UserInfo = Depends(get_current_user_or_agent),
    db: aiosqlite.Connection = Depends(get_db),
) -> InboxStatsResponse:
    return await get_inbox_stats(
        db,
        user,
        needs_triage=needs_triage,
        classified=classified,
        source=source,
        program=program,
        topic=topic,
        treatment=treatment,
        status=item_status,
    )


@router.get("/items/unread-count")
async def get_inbox_unread_count(
    user: UserInfo = Depends(get_current_user_or_agent),
    db: aiosqlite.Connection = Depends(get_db),
) -> dict:
    count = await get_unread_count(db, user)
    return {"count": count}


@router.get("/digest/current")
async def get_current_digest_items(
    domain_key: str | None = Query(None),
    limit: int = Query(200, ge=1, le=500),
    user: UserInfo = Depends(get_current_user_or_agent),
    db: aiosqlite.Connection = Depends(get_db),
) -> list[dict]:
    workspace_id = user.workspace_id or "ws_default"
    return await list_digest_items(
        db,
        workspace_id,
        state="visible",
        domain_key=domain_key,
        limit=limit,
    )


@router.get("/digest/overflow")
async def get_current_digest_overflow(
    domain_key: str | None = Query(None),
    limit: int = Query(200, ge=1, le=500),
    user: UserInfo = Depends(require_role("operator", "admin", "super_admin")),
    db: aiosqlite.Connection = Depends(get_db),
) -> list[dict]:
    workspace_id = user.workspace_id or "ws_default"
    return await list_digest_items(
        db,
        workspace_id,
        state="overflow",
        domain_key=domain_key,
        limit=limit,
    )


@router.get("/digest/stats")
async def get_current_digest_stats(
    user: UserInfo = Depends(require_role("operator", "admin", "super_admin")),
    db: aiosqlite.Connection = Depends(get_db),
) -> dict:
    workspace_id = user.workspace_id or "ws_default"
    return await get_digest_stats(db, workspace_id)


@router.post("/digest/recompute")
async def recompute_digest(
    user: UserInfo = Depends(require_role("admin", "super_admin")),
) -> dict:
    workspace_id = user.workspace_id or "ws_default"
    return await recompute_digest_now(workspace_id)


@router.patch("/items/{inbox_item_id}/status")
async def patch_inbox_status(
    inbox_item_id: str,
    body: InboxStatusUpdateRequest,
    user: UserInfo = Depends(require_role("operator", "admin", "super_admin")),
    db: aiosqlite.Connection = Depends(get_write_db),
) -> dict:
    return await update_inbox_status(db, user, inbox_item_id, body)


@router.patch(
    "/items/{inbox_item_id}/taxonomy", response_model=InboxTaxonomyUpdateResponse
)
async def patch_inbox_taxonomy(
    inbox_item_id: str,
    body: InboxTaxonomyUpdateRequest,
    user: UserInfo = Depends(get_current_user_or_agent),
    db: aiosqlite.Connection = Depends(get_write_db),
) -> InboxTaxonomyUpdateResponse:
    return await update_inbox_taxonomy(db, user, inbox_item_id, body)


@router.get("/items/{inbox_item_id}", response_model=InboxItemDetail)
async def get_inbox_item(
    inbox_item_id: str,
    user: UserInfo = Depends(get_current_user_or_agent),
    db: aiosqlite.Connection = Depends(get_db),
) -> InboxItemDetail:
    return await get_inbox_item_detail(db, user, inbox_item_id)


@router.post(
    "/items/{inbox_item_id}/decision", response_model=InboxTriageDecisionResponse
)
async def submit_inbox_decision(
    inbox_item_id: str,
    body: InboxTriageDecisionRequest,
    user: UserInfo = Depends(require_role("operator", "admin", "super_admin")),
    db: aiosqlite.Connection = Depends(get_write_db),
) -> InboxTriageDecisionResponse:
    return await apply_triage_decision(db, user, inbox_item_id, body)


@router.post("/items/{inbox_item_id}/tldr")
async def generate_inbox_tldr(
    inbox_item_id: str,
    user: UserInfo = Depends(require_role("operator", "admin", "super_admin")),
) -> dict:
    workspace_id = user.workspace_id or "ws_default"
    return await get_or_generate_tldr(inbox_item_id, workspace_id)


@router.post("/items/{inbox_item_id}/deep-research")
async def generate_inbox_deep_research(
    inbox_item_id: str,
    force: bool = False,
    user: UserInfo = Depends(require_role("operator", "admin", "super_admin")),
) -> dict:
    workspace_id = user.workspace_id or "ws_default"
    return await get_or_generate_deep_research(inbox_item_id, workspace_id, force=force)


class ReclassifyRequest(BaseModel):
    source: str | None = Field(None, description="Filter by source name")
    status: str = Field("unread", description="Filter by status (default: unread)")
    limit: int = Field(100, ge=1, le=500, description="Max items to reclassify")


@router.post("/reclassify")
async def reclassify_inbox_items(
    body: ReclassifyRequest,
    user: UserInfo = Depends(require_role("admin", "super_admin")),
    db: aiosqlite.Connection = Depends(get_write_db),
) -> dict:
    """Queue matching inbox items for re-classification. Admin only, fire-and-forget."""
    workspace_id = user.workspace_id or "ws_default"

    where_clauses = ["workspace_id = ?"]
    params: list[object] = [workspace_id]

    if body.source:
        where_clauses.append("source = ?")
        params.append(body.source)
    if body.status:
        where_clauses.append("status = ?")
        params.append(body.status)

    where_sql = " AND ".join(where_clauses)
    params.append(body.limit)

    cursor = await db.execute(
        f"SELECT id FROM inbox_items WHERE {where_sql} ORDER BY created_at DESC LIMIT ?",
        tuple(params),
    )
    rows = await cursor.fetchall()

    queued = 0
    for row in rows:
        try:
            schedule_classification(settings.db_path, workspace_id, row["id"])
            queued += 1
        except Exception:
            logger.exception("Failed to schedule reclassify for item %s", row["id"])

    logger.info(
        "Reclassify queued %d/%d items (source=%s, status=%s) by %s",
        queued,
        len(rows),
        body.source,
        body.status,
        user.username,
    )
    return {"queued": queued}


@router.get("/source-scores")
async def get_source_scores_route(
    user: UserInfo = Depends(get_current_user_or_agent),
    db: aiosqlite.Connection = Depends(get_db),
) -> list[dict]:
    workspace_id = user.workspace_id or "ws_default"
    return await get_source_scores(db, workspace_id)


@router.get(
    "/gmail-sync/candidates",
    response_model=list[InboxGmailSyncCandidate],
)
async def get_gmail_sync_candidates(
    limit: int = Query(50, ge=1, le=200),
    user: UserInfo = Depends(require_agent_token_scope("inbox:ingest")),
    db: aiosqlite.Connection = Depends(get_db),
) -> list[InboxGmailSyncCandidate]:
    workspace_id = user.workspace_id or "ws_default"
    return await list_gmail_sync_candidates(db, workspace_id, limit=limit)


@router.post("/gmail-sync/{inbox_item_id}/complete")
async def complete_gmail_sync(
    inbox_item_id: str,
    body: InboxGmailSyncCompleteRequest,
    user: UserInfo = Depends(require_agent_token_scope("inbox:ingest")),
    db: aiosqlite.Connection = Depends(get_write_db),
) -> dict:
    return await mark_gmail_sync_complete(db, user, inbox_item_id, body)
