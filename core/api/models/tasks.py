# v1.3.0 - 2026-04-11 - Add completion_mode for non-PR task lifecycle
from __future__ import annotations

from pydantic import BaseModel, Field

from core.api.models.common import CommentResponse, StatusCounts, UserSummary


VALID_STATUSES = {
    "pending",
    "approved",
    "in_progress",
    "review",
    "completed",
    "rejected",
    "failed",
}
VALID_PRIORITIES = {"high", "medium", "low"}
VALID_KINDS = {"normal", "idea"}
VALID_SOURCES = {
    "reflectx_proposal",
    "telegram",
    "manual",
    "session",
    "console",
    "rem_proposal",
}

VALID_TRANSITIONS: dict[str, set[str]] = {
    "pending": {"approved", "rejected"},
    "approved": {"in_progress", "rejected"},
    "in_progress": {"completed", "failed", "review", "rejected"},
    "review": {"completed", "in_progress"},
    "failed": {"approved"},
}

VALID_DELEGATIONS = {"agent", "hybrid", "human"}

# Completion mode — drives the guard in validate_and_transition_task.
#   pr   (default): code/system projects require a merged PR to complete
#   doc  : research/brainstorm/plan — completion via doc/handoff, no PR needed
#   none : verify/diagnose/free transition — no backend check
VALID_COMPLETION_MODES = {"pr", "doc", "none"}


class TaskCreateRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)
    description: str | None = Field(None, max_length=10000)
    project: str = Field(..., min_length=1, max_length=50)
    kind: str = Field("normal", pattern=r"^(normal|idea)$")
    priority: str = Field("medium", pattern=r"^(high|medium|low)$")
    source: str = Field(
        ...,
        pattern=r"^(reflectx_proposal|telegram|manual|session|console|rem_proposal)$",
    )
    source_ref: str | None = Field(None, max_length=500)
    owner_id: str | None = Field(
        None, max_length=50
    )  # users.id (previously assigned_to)
    tags: list[str] = Field(default_factory=list, max_length=10)
    # ICE-D scoring fields (optional on create)
    impact: int | None = Field(None, ge=1, le=10)
    confidence: int | None = Field(None, ge=1, le=10)
    ease: int | None = Field(None, ge=1, le=10)
    delegation: str | None = Field(None, pattern=r"^(agent|hybrid|human)$")
    # Reminder
    due_date: str | None = Field(
        None, max_length=10, description="ISO 8601 date (YYYY-MM-DD)"
    )
    # Completion mode: pr (default, requires merged PR) | doc | none
    completion_mode: str = Field("pr", pattern=r"^(pr|doc|none)$")


class TaskUpdateRequest(BaseModel):
    title: str | None = Field(None, min_length=1, max_length=200)
    description: str | None = Field(None, max_length=10000)
    status: str | None = Field(
        None,
        pattern=r"^(pending|approved|in_progress|review|completed|rejected|failed)$",
    )
    kind: str | None = Field(None, pattern=r"^(normal|idea)$")
    priority: str | None = Field(None, pattern=r"^(high|medium|low)$")
    owner_id: str | None = None  # users.id (previously assigned_to)
    tags: list[str] | None = None
    # ICE-D scoring fields
    impact: int | None = Field(None, ge=1, le=10)
    confidence: int | None = Field(None, ge=1, le=10)
    ease: int | None = Field(None, ge=1, le=10)
    delegation: str | None = Field(None, pattern=r"^(agent|hybrid|human)$")
    # Reminder
    due_date: str | None = Field(
        None, max_length=10, description="ISO 8601 date (YYYY-MM-DD)"
    )
    # Completion mode
    completion_mode: str | None = Field(None, pattern=r"^(pr|doc|none)$")


class TaskListResponse(BaseModel):
    """Lightweight task representation for list endpoints (Triage board, GET /tasks)."""

    id: str
    title: str
    kind: str
    status: str
    project: str
    priority: str
    created_by: str
    owner_id: str | None = None  # users.id — kept as plain string, no embedded object
    source: str
    source_ref: str | None = None
    tags: list[str] = Field(default_factory=list)
    deleted_at: str | None = None
    created_at: str
    updated_at: str
    # ICE-D scoring fields
    impact: int | None = None
    confidence: int | None = None
    ease: int | None = None
    delegation: str | None = None
    ice_score: int | None = None
    scored_by: str | None = None
    scored_at: str | None = None
    # PR status — included in list so Triage can show Merge vs Complete
    pr_status: str | None = None
    # Reminder fields
    due_date: str | None = None
    reminder_sent_at: str | None = None
    # Completion mode (pr|doc|none) — drives transition guard
    completion_mode: str = "pr"


class TaskDetailResponse(TaskListResponse):
    """Full task representation for detail/update endpoints. Extends TaskListResponse with heavy fields."""

    owner: UserSummary | None = None  # embedded user summary, None if unassigned
    review_feedback: str | None = None
    description: str | None = None
    comments: list[CommentResponse] | None = None  # populated when ?detailed=true
    kg_context: dict | None = None  # populated when ?deep=true (Phase 7.0)


# Backward compatibility alias
TaskResponse = TaskDetailResponse


class ProjectSummary(BaseModel):
    project: str
    open_count: int
    total_count: int


class ProjectStatusBreakdown(BaseModel):
    project: str
    pending: int = 0
    approved: int = 0
    in_progress: int = 0
    review: int = 0


class TaskSummary(BaseModel):
    total: int
    by_status: StatusCounts
    by_project: list[ProjectStatusBreakdown]
    by_priority: dict[str, int]


# --- Merge Conflict Detection Models ---


class MergeConflictEntry(BaseModel):
    task_id: str
    pr_created_at: str
    merge_position: int
    can_merge: bool
    blocked_by: str | None = None


class MergeConflictGroup(BaseModel):
    migration_number: int
    tasks: list[MergeConflictEntry]


class MergeConflictResponse(BaseModel):
    conflicts: list[MergeConflictGroup]
