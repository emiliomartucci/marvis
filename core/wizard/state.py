"""Wizard state machine — Pydantic models, serializable across CLI + Console."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


class StepId(str, Enum):
    welcome = "welcome"
    storage = "storage"
    llm_provider = "llm_provider"
    first_project = "first_project"
    recap = "recap"


class LlmProvider(str, Enum):
    anthropic = "anthropic"
    openai = "openai"
    mac_gateway = "mac_gateway"
    bedrock = "bedrock"


class DbBackend(str, Enum):
    sqlite = "sqlite"
    postgres = "postgres"


class ProjectType(str, Enum):
    code = "code"
    work = "work"
    system = "system"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class WelcomePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bsl_accepted: bool = False
    accepted_at: datetime | None = None


class StoragePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    projects_root: str
    db_backend: DbBackend = DbBackend.sqlite
    db_path: str | None = None
    postgres_dsn: str | None = None


class LlmProviderPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: LlmProvider | None = None
    api_key: str | None = None
    base_url: str | None = None
    test_passed: bool = False


class FirstProjectPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    slug: str
    type: ProjectType = ProjectType.code


class WizardState(BaseModel):
    """Serializable snapshot of wizard progress.

    Lives in localStorage (Console) or memory (CLI) until finalize, then
    materializes into settings.yaml + byok.vault + project.yaml.
    """

    model_config = ConfigDict(extra="forbid")

    version: str = "1.0"
    current_step: StepId = StepId.welcome
    completed_steps: list[StepId] = Field(default_factory=list)
    skipped_steps: list[StepId] = Field(default_factory=list)
    started_at: datetime = Field(default_factory=_utcnow)
    completed_at: datetime | None = None
    welcome: WelcomePayload = Field(default_factory=WelcomePayload)
    storage: StoragePayload | None = None
    llm_provider: LlmProviderPayload | None = None
    first_project: FirstProjectPayload | None = None

    def mark_completed(self, step: StepId) -> None:
        if step not in self.completed_steps:
            self.completed_steps.append(step)
        if step in self.skipped_steps:
            self.skipped_steps.remove(step)

    def mark_skipped(self, step: StepId) -> None:
        if step not in self.skipped_steps:
            self.skipped_steps.append(step)
        if step in self.completed_steps:
            self.completed_steps.remove(step)

    def is_finalized(self) -> bool:
        return self.completed_at is not None

    def step_status(self, step: StepId) -> str:
        """Returns one of: active | completed | skipped | pending."""
        if step in self.completed_steps:
            return "completed"
        if step in self.skipped_steps:
            return "skipped"
        if self.current_step == step and not self.is_finalized():
            return "active"
        return "pending"
