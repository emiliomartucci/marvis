# v1.0.0 - 2026-03-03 - Cost tracking and billing models
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ProjectCostSummary(BaseModel):
    project_slug: str
    program: str | None = None
    total_cost_usd: float = 0.0
    conversation_count: int = 0


class ConversationCost(BaseModel):
    conversation_id: str
    session_name: str | None = None
    display_name: str | None = None
    model: str | None = None
    cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    message_count: int = 0
    working_seconds: int = 0
    created_at: str | None = None
    completed_at: str | None = None
    updated_at: str | None = None


class TaskCostEntry(BaseModel):
    id: str
    task_id: str
    entry_type: str   # 'agent' | 'human'
    source: str       # 'task_completed' | 'manual'
    conversation_id: str | None = None
    pr_id: str | None = None
    cost_usd_delta: float = 0.0
    agent_seconds: int = 0
    human_minutes: float = 0.0
    total_cost_usd: float = 0.0
    total_bill_usd: float = 0.0
    is_billable: bool = True
    billable_reason: str | None = None
    description: str | None = None
    created_by: str
    created_at: str

    model_config = ConfigDict(extra="ignore")


class TaskCostSummary(BaseModel):
    task_id: str
    total_cost_usd: float = 0.0
    total_bill_usd: float = 0.0
    agent_cost_usd: float = 0.0
    human_cost_usd: float = 0.0
    billable_usd: float = 0.0
    non_billable_usd: float = 0.0
    entry_count: int = 0
    entries: list[TaskCostEntry] = []
    created_entry_id: str | None = None


class HumanCostEntryCreate(BaseModel):
    human_minutes: float = Field(..., gt=0, le=1440)  # max 24h
    description: str | None = Field(None, max_length=500)
    is_billable: bool = True
    idempotency_key: str | None = Field(None, max_length=128)

    @field_validator("human_minutes")
    @classmethod
    def validate_minutes(cls, v: float) -> float:
        if v < 1.0:
            raise ValueError("human_minutes must be at least 1 minute")
        return round(v, 2)


class ProjectBillingSummary(BaseModel):
    project_slug: str
    from_date: str
    to_date: str
    total_cost_usd: float = 0.0
    total_bill_usd: float = 0.0
    agent_cost_usd: float = 0.0
    human_cost_usd: float = 0.0
    billable_usd: float = 0.0
    non_billable_usd: float = 0.0
    task_count: int = 0
    entry_count: int = 0
    token_markup_factor: float = 1.0
    agent_bill_rate: float = 0.0
    human_bill_rate: float = 0.0
