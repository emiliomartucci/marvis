"""Authenticated compatibility tombstones for retired integrations.

The n8n automation proxy and reddit-farmer are no longer MarvisX services.
Their last public HTTP contract remains registered for one N/N-1 compatibility
window so existing clients receive an explicit ``410 Gone`` instead of an
ambiguous ``404``. No retired service, database, configuration, or worker is
imported here.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal, NoReturn

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from core.api.models import UserInfo
from core.api.rbac import require_role


class TriggerRequest(BaseModel):
    data: dict[str, Any] | None = None


class ValidationFlag(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    rule: str
    pass_: bool = Field(alias="pass")
    hint: str | None = None


class ThreadContext(BaseModel):
    id: str
    subreddit: str
    title: str
    body: str
    score: int
    num_comments: int
    url: str | None = None
    created_at: datetime | None = None
    selector_score: float | None = None


class DraftSummary(BaseModel):
    id: int
    thread_id: str
    subreddit: str
    title: str
    status: Literal["pending", "approved", "rejected", "posted", "failed"]
    llm_model: str | None = None
    persona_id: str | None = None
    created_at: datetime
    flags_failed_count: int
    has_edit: bool


class DraftDetail(DraftSummary):
    body: str
    body_edited: str | None = None
    validation_flags: list[ValidationFlag] = Field(default_factory=list)
    thread: ThreadContext
    rejection_reason: str | None = None
    scheduled_at: datetime | None = None
    posted_at: datetime | None = None
    posted_comment_id: str | None = None


class QueueListResponse(BaseModel):
    items: list[DraftSummary]
    total: int | None = None
    limit: int
    offset: int


class ApproveRequest(BaseModel):
    schedule_hint: datetime | None = None


class EditRequest(BaseModel):
    body: str = Field(min_length=1, max_length=10000)


class RejectRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=500)


class ActionResponse(BaseModel):
    id: int
    status: str
    audit_id: str | None = None


class EditResponse(BaseModel):
    id: int
    status: str
    validation_flags: list[ValidationFlag]


class Metrics(BaseModel):
    pending_count: int
    posted_last_24h: int
    karma_total: int | None = None
    total_cost_usd_mtd: float = 0.0
    last_scrape_at: datetime | None = None
    shadowban_status: bool | None = None


def _gone(integration: str) -> NoReturn:
    raise HTTPException(
        status_code=410,
        detail={
            "code": "integration_retired",
            "integration": integration,
            "message": f"The {integration} integration has been retired.",
        },
    )


_RETIRED_RESPONSE = {410: {"description": "Integration retired"}}
_Operator = Annotated[UserInfo, Depends(require_role("operator"))]
_Admin = Annotated[UserInfo, Depends(require_role("admin"))]
_RedditOperator = Annotated[
    UserInfo, Depends(require_role("operator", "admin", "super_admin"))
]

router = APIRouter()


@router.get(
    "/api/v1/automations",
    tags=["automations"],
    deprecated=True,
    operation_id="list_automations_api_v1_automations_get",
    responses=_RETIRED_RESPONSE,
)
async def _list_automations(_user: _Operator):
    """Return an explicit retirement response for the old n8n proxy."""
    _gone("n8n")


@router.post(
    "/api/v1/automations/{workflow_id}/trigger",
    tags=["automations"],
    deprecated=True,
    operation_id="trigger_automation_api_v1_automations__workflow_id__trigger_post",
    responses=_RETIRED_RESPONSE,
)
async def _trigger_automation(
    workflow_id: str,
    _user: _Admin,
    body: TriggerRequest | None = None,
):
    """Return an explicit retirement response for the old n8n trigger."""
    _gone("n8n")


@router.get(
    "/api/v1/automations/executions",
    tags=["automations"],
    deprecated=True,
    operation_id="list_executions_api_v1_automations_executions_get",
    responses=_RETIRED_RESPONSE,
)
async def _list_executions(
    _user: _Operator,
    workflow_id: str | None = None,
    status: str | None = None,
    limit: int = Query(default=20, le=100),
):
    """Return an explicit retirement response for old n8n executions."""
    _gone("n8n")


@router.get(
    "/api/v1/automations/events",
    tags=["automations"],
    deprecated=True,
    operation_id="list_events_api_v1_automations_events_get",
    responses=_RETIRED_RESPONSE,
)
async def _list_events(
    _user: _Operator,
    event_type: str | None = None,
    project: str | None = None,
    dispatched: bool | None = None,
    limit: int = Query(default=50, le=200),
):
    """Return an explicit retirement response for the old events proxy."""
    _gone("n8n")


@router.get(
    "/api/v1/reddit/queue",
    tags=["reddit"],
    response_model=QueueListResponse,
    deprecated=True,
    operation_id="list_queue_api_v1_reddit_queue_get",
    responses=_RETIRED_RESPONSE,
)
async def _list_queue(
    _user: _RedditOperator,
    status: str | None = Query(
        default=None, pattern="^(pending|approved|rejected|posted|failed)$"
    ),
    subreddit: str | None = Query(default=None, min_length=1, max_length=50),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> QueueListResponse:
    """Return an explicit retirement response for the old draft queue."""
    _gone("reddit-farmer")


@router.get(
    "/api/v1/reddit/queue/{draft_id}",
    tags=["reddit"],
    response_model=DraftDetail,
    deprecated=True,
    operation_id="get_queue_item_api_v1_reddit_queue__draft_id__get",
    responses=_RETIRED_RESPONSE,
)
async def _get_queue_item(
    draft_id: int,
    _user: _RedditOperator,
) -> DraftDetail:
    """Return an explicit retirement response for an old queue item."""
    _gone("reddit-farmer")


@router.post(
    "/api/v1/reddit/queue/{draft_id}/approve",
    tags=["reddit"],
    response_model=ActionResponse,
    deprecated=True,
    operation_id="approve_draft_api_v1_reddit_queue__draft_id__approve_post",
    responses=_RETIRED_RESPONSE,
)
async def _approve_draft(
    draft_id: int,
    body: ApproveRequest,
    _user: _RedditOperator,
) -> ActionResponse:
    """Return an explicit retirement response for old queue approval."""
    _gone("reddit-farmer")


@router.post(
    "/api/v1/reddit/queue/{draft_id}/edit",
    tags=["reddit"],
    response_model=EditResponse,
    deprecated=True,
    operation_id="edit_draft_api_v1_reddit_queue__draft_id__edit_post",
    responses=_RETIRED_RESPONSE,
)
async def _edit_draft(
    draft_id: int,
    body: EditRequest,
    _user: _RedditOperator,
) -> EditResponse:
    """Return an explicit retirement response for old queue editing."""
    _gone("reddit-farmer")


@router.post(
    "/api/v1/reddit/queue/{draft_id}/reject",
    tags=["reddit"],
    response_model=ActionResponse,
    deprecated=True,
    operation_id="reject_draft_api_v1_reddit_queue__draft_id__reject_post",
    responses=_RETIRED_RESPONSE,
)
async def _reject_draft(
    draft_id: int,
    body: RejectRequest,
    _user: _RedditOperator,
) -> ActionResponse:
    """Return an explicit retirement response for old queue rejection."""
    _gone("reddit-farmer")


@router.get(
    "/api/v1/reddit/metrics",
    tags=["reddit"],
    response_model=Metrics,
    deprecated=True,
    operation_id="get_metrics_api_v1_reddit_metrics_get",
    responses=_RETIRED_RESPONSE,
)
async def _get_metrics(
    _user: _RedditOperator,
) -> Metrics:
    """Return an explicit retirement response for old farmer metrics."""
    _gone("reddit-farmer")


__all__ = ["router"]
