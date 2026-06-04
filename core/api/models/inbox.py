from __future__ import annotations

from typing import Any, Final

from pydantic import BaseModel, Field, field_validator, model_validator

# Canonical set of valid inbox item statuses. Single source of truth —
# import this instead of hard-coding regex tuples in multiple modules.
# 'preferred' (PR A, 2026-04-11) is the "gold private" signal (+2 in scoring),
# distinct from 'newsletter' which is the "gold public" signal (+3).
VALID_INBOX_STATUSES: Final[frozenset[str]] = frozenset(
    {
        "unread",
        "read",
        "saved",
        "idea",
        "newsletter",
        "preferred",
        "auto_ignored",
        "ignored",
    }
)

VALID_DIGEST_SELECTION_STATES: Final[frozenset[str]] = frozenset(
    {
        "visible",
        "overflow",
        "expired",
    }
)


class InboxIngestRequest(BaseModel):
    source: str = Field(
        ..., min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9&+_.\-]{0,63}$"
    )
    source_item_id: str | None = Field(None, max_length=255)
    title: str | None = Field(None, max_length=4000)
    content: str | None = Field(None, max_length=100000)
    url: str | None = Field(None, max_length=4000)
    published_at: str | None = Field(None, max_length=255)
    source_path: str | None = Field(None, max_length=4000)
    metadata: dict[str, Any] = Field(default_factory=dict)
    candidate_programs: list[str] = Field(default_factory=list, max_length=20)
    default_program: str | None = Field(None, max_length=64)
    topic: str | None = Field(None, pattern=r"^[a-z-]{2,64}$")
    treatment: str | None = Field(None, pattern=r"^[a-z-]{2,64}$")


class InboxIngestResponse(BaseModel):
    id: str
    source: str
    deduplicated: bool
    status: str
    title: str | None = None
    source_item_id: str | None = None
    source_path: str | None = None
    candidate_programs: list[str] = Field(default_factory=list)
    default_program: str | None = None
    topic: str = "general"
    treatment: str = "read"
    created_at: str


# Max items per batch. Sized so the writer holds _write_lock briefly:
# 500 INSERT + 1 commit ~= 100-300ms on NVMe, which is comparable to a
# single large commit and dwarfed by the savings vs 500 separate commits
# (500 fsync, 500 lock acquisitions).
INBOX_INGEST_BATCH_MAX = 500


class InboxIngestBatchRequest(BaseModel):
    items: list[InboxIngestRequest] = Field(
        ..., min_length=1, max_length=INBOX_INGEST_BATCH_MAX
    )


class InboxIngestBatchItemError(BaseModel):
    index: int
    source: str | None = None
    source_item_id: str | None = None
    error: str


class InboxIngestBatchResponse(BaseModel):
    inserted: list[InboxIngestResponse] = Field(default_factory=list)
    deduplicated: list[InboxIngestResponse] = Field(default_factory=list)
    errors: list[InboxIngestBatchItemError] = Field(default_factory=list)
    counts: dict[str, int] = Field(default_factory=dict)


class InboxTriageDecisionRequest(BaseModel):
    decision: str = Field(
        ..., pattern=r"^(ignore|keep|needs_human_review|create_idea|create_task)$"
    )
    confidence: float = Field(..., ge=0.0, le=1.0)
    reason: str = Field(..., min_length=1, max_length=4000)
    target_program: str | None = Field(None, min_length=1, max_length=100)
    target_project: str | None = Field(None, min_length=1, max_length=100)
    task_kind: str | None = Field(None, pattern=r"^(idea|normal)$")
    task_title: str | None = Field(None, min_length=1, max_length=200)
    task_description: str | None = Field(None, max_length=10000)
    linked_task_id: str | None = Field(None, max_length=100)
    tags: list[str] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def validate_create_decisions(self) -> "InboxTriageDecisionRequest":
        if self.decision not in {"create_idea", "create_task"}:
            return self
        if not self.target_program:
            raise ValueError("target_program is required for create_idea/create_task")
        if not self.linked_task_id:
            if not self.target_project:
                raise ValueError("target_project is required when creating a new task")
            if not self.task_title:
                raise ValueError("task_title is required when creating a new task")
        return self


class InboxTriageDecisionResponse(BaseModel):
    inbox_item_id: str
    decision: str
    confidence: float
    reason: str
    target_program: str | None = None
    target_project: str | None = None
    task_kind: str | None = None
    task_title: str | None = None
    task_description: str | None = None
    linked_task_id: str | None = None
    tags: list[str] = Field(default_factory=list)
    decided_by: str
    created_at: str
    updated_at: str


class InboxTaxonomyUpdateRequest(BaseModel):
    topic: str = Field(
        ...,
        pattern=r"^(ai-news|ai-products|tooling|security-devtools|pv-energy|strategy-business|policy-politics|general)$",
    )
    treatment: str = Field(..., pattern=r"^(read|save|read_save|ignore)$")
    confidence: float | None = Field(None, ge=0.0, le=1.0)
    note: str | None = Field(None, max_length=2000)


class InboxTaxonomyUpdateResponse(BaseModel):
    inbox_item_id: str
    topic: str
    treatment: str
    updated_at: str


class InboxStatusUpdateRequest(BaseModel):
    status: str
    ignore_reason: str | None = Field(
        None,
        pattern=r"^(duplicate|spam|not_interested|not_relevant|custom)$",
    )

    @field_validator("status")
    @classmethod
    def _validate_status(cls, v: str) -> str:
        if v not in VALID_INBOX_STATUSES:
            raise ValueError(
                f"invalid status {v!r}, must be one of {sorted(VALID_INBOX_STATUSES)}"
            )
        return v

    @model_validator(mode="after")
    def validate_ignore_reason(self) -> "InboxStatusUpdateRequest":
        if self.status == "ignored" and not self.ignore_reason:
            raise ValueError("ignore_reason is required when status is 'ignored'")
        if self.ignore_reason and self.status != "ignored":
            raise ValueError("ignore_reason is only valid when status is 'ignored'")
        return self


class InboxGmailSyncCandidate(BaseModel):
    inbox_item_id: str
    gmail_message_id: str
    topic: str = "general"
    treatment: str = "read"
    status: str = "unread"
    confidence: float | None = None
    add_labels: list[str] = Field(default_factory=list)
    remove_unread: bool = False


class InboxGmailSyncCompleteRequest(BaseModel):
    labels_applied: list[str] = Field(default_factory=list, max_length=20)
    removed_unread: bool = False


class InboxItemSummary(BaseModel):
    id: str
    source_type: str | None = None
    source_label: str | None = None
    external_id: str | None = None
    title: str | None = None
    snippet: str | None = None
    sender: str | None = None
    url: str | None = None
    program: str | None = None
    project: str | None = None
    topic: str = "general"
    treatment: str = "read"
    status: str = "unread"
    ignore_reason: str | None = None
    received_at: str | None = None
    needs_triage: bool = True
    triage: InboxTriageDecisionResponse | None = None


class InboxItemDetail(InboxItemSummary):
    content: str | None = None
    raw_payload: Any | None = None
    tldr: str | None = None
    deep_research: str | None = None


class InboxStatsResponse(BaseModel):
    total: int
    ideas: int
    tasks: int
    review: int
    unread: int = 0
    read: int = 0
    saved: int = 0
    ignored: int = 0
