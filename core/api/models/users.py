# v1.1.0 - 2026-03-11 - Add linux_username, provisioned_at, onboarding_completed fields (Fase 3)
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from core.api.models.common import UserSummary  # noqa: F401 (re-exported for convenience)


# --- Users ---

class UserCreateRequest(BaseModel):
    slug: str = Field(..., min_length=1, max_length=50, pattern=r"^[a-z0-9_-]+$")
    display_name: str = Field(..., min_length=1, max_length=100)
    type: Literal["human", "agent"] = "human"
    email: str | None = None
    avatar_color: str | None = None
    system_role: Literal["admin", "operator", "viewer", "super_admin"] = "viewer"
    notification_channels: list[str] = Field(default_factory=list)
    telegram_chat_id: str | None = None


class UserUpdateRequest(BaseModel):
    display_name: str | None = Field(None, min_length=1, max_length=100)
    email: str | None = None
    avatar_color: str | None = None
    notification_channels: list[str] | None = None
    telegram_chat_id: str | None = None
    system_role: str | None = None
    # Fase 3 — Linux provisioning (admin+ only)
    linux_username: str | None = None
    provisioned_at: str | None = None
    onboarding_completed: bool | None = None


class UserTeamSummary(BaseModel):
    id: str
    slug: str
    display_name: str
    role: Literal["member", "admin"] = "member"


class UserResponse(BaseModel):
    id: str
    slug: str
    display_name: str
    type: str
    email: str | None = None
    avatar_color: str
    system_role: str
    notification_channels: list[str] = Field(default_factory=list)
    telegram_chat_id: str | None = None
    last_used_at: str | None = None
    deleted_at: str | None = None
    created_at: str
    updated_at: str
    teams: list[UserTeamSummary] = Field(default_factory=list)
    # Fase 3 — Linux provisioning
    linux_username: str | None = None
    provisioned_at: str | None = None
    onboarding_completed: bool = False


# --- RACI ---

RaciRole = Literal["responsible", "accountable", "consulted", "informed"]


class RaciEntry(BaseModel):
    user: UserSummary
    role: RaciRole


class RaciAddRequest(BaseModel):
    user_id: str = Field(..., min_length=1, max_length=50)
    role: RaciRole
    reason: str | None = Field(None, max_length=500)


class RaciReplaceRequest(BaseModel):
    """PUT /raci — replace completo idempotente."""
    entries: list[RaciAddRequest]


# --- Agent Registry Models ---

AgentType = Literal["project", "system", "digital_copy"]
AgentStatus = Literal["active", "inactive", "error"]
AgentModel = Literal["haiku", "sonnet", "opus"]
RunStatus = Literal["running", "success", "error", "timeout", "killed"]
RunTrigger = Literal["cron", "manual", "api"]
AgentFileType = Literal["SOUL.md", "TOOLS.md", "IDENTITY.md"]

AGENT_FILE_ALLOWLIST: frozenset[str] = frozenset({"SOUL.md", "TOOLS.md", "IDENTITY.md"})


class AgentResponse(BaseModel):
    id: str
    user_id: str
    scheduler_agent_id: str | None
    agent_type: AgentType
    project_slug: str | None
    model: AgentModel
    status: AgentStatus
    enabled: int
    soul_path: str | None
    tools_path: str | None
    identity_path: str | None
    description: str | None
    deleted_at: str | None
    created_at: str
    updated_at: str
    # Computed
    display_name: str | None = None
    schedule_count: int = 0
    active_run_count: int = 0
    total_cost_usd: float = 0.0


class AgentCreateRequest(BaseModel):
    display_name: str = Field(min_length=1, max_length=200)
    scheduler_agent_id: str = Field(pattern=r'^[a-z0-9][a-z0-9-]*$', max_length=64)
    agent_type: AgentType
    model: AgentModel = "haiku"
    project_slug: str | None = None
    description: str | None = Field(default=None, max_length=1000)
    soul_content: str | None = Field(default=None, max_length=102400)
    avatar_color: str | None = None


class AgentUpdateRequest(BaseModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=200)
    model: AgentModel | None = None
    status: AgentStatus | None = None
    enabled: int | None = None
    project_slug: str | None = None
    description: str | None = Field(default=None, max_length=1000)


class ScheduleResponse(BaseModel):
    id: str
    agent_id: str
    name: str
    cron_expr: str
    cron_tz: str
    prompt: str | None
    timeout_seconds: int
    enabled: int
    scheduler_job_id: str | None
    last_run_at: str | None
    last_run_status: str | None
    created_at: str
    updated_at: str


class ScheduleCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    cron_expr: str = Field(max_length=100)
    cron_tz: str = "Europe/Rome"
    prompt: str | None = Field(default=None, max_length=10000)
    timeout_seconds: int = 120


class RunResponse(BaseModel):
    id: str
    agent_id: str
    schedule_id: str | None
    trigger: RunTrigger
    session_uuid: str | None
    started_at: str
    finished_at: str | None
    status: RunStatus
    summary: str | None
    cost_usd: float | None
    input_tokens: int | None
    output_tokens: int | None
    model: str | None
    log_size_bytes: int | None
    error_message: str | None


class RunDetailResponse(RunResponse):
    log_tail: str | None
