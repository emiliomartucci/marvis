# v1.2.0 - 2026-03-09 - Add ProjectCreateRequest/Response models
from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from core.api.models.common import StatusCounts


ProjectType = Literal["work", "code", "system"]
ProjectStatus = Literal["active", "paused", "blocked", "completed", "not_started"]
ProjectLifecycle = Literal["idea", "planning", "active", "maintenance", "archived"]
_HEX_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


class HandoffEntry(BaseModel):
    filename: str
    date: str
    summary: str
    session: str | None = None
    branch: str | None = None
    tags: list[str] = []


class HandoffSearchResult(BaseModel):
    project: str
    file: str          # filename only, e.g. "handoff-2026-03-06-devx-cost.md"
    date: str | None
    session: str | None
    tags: list[str]
    branch: str | None
    snippet: str       # ~300 chars around the match (or start of body)
    score: int         # number of matches in the file (for ranking)


class DocEntry(BaseModel):
    filename: str
    date: str | None = None
    title: str | None = None
    category: str | None = None


class ProjectInfo(BaseModel):
    slug: str
    name: str
    program: str | None = None
    language: str | None = None
    lifecycle: str | None = None
    phase: str | None = None
    scope: str | None = None
    description: str | None = None
    type: ProjectType | None = None
    repo_path: str | None = None
    metadata_path: str | None = None
    status: ProjectStatus | None = None
    color: str | None = None
    task_counts: StatusCounts = StatusCounts()
    last_handoff: str | None = None
    last_status_update: str | None = None
    on_server: bool = True
    path: str | None = None


class ProjectDetail(BaseModel):
    slug: str
    name: str
    program: str | None = None
    language: str | None = None
    lifecycle: str | None = None
    phase: str | None = None
    scope: str | None = None
    description: str | None = None
    type: ProjectType | None = None
    repo_path: str | None = None
    metadata_path: str | None = None
    context_md: str | None = None
    config: dict = {}
    deploy: dict | None = None
    color: str | None = None
    handoffs: list[HandoffEntry] = []
    plans: list[DocEntry] = []
    brainstorms: list[DocEntry] = []
    solutions: list[DocEntry] = []
    kg_context: dict | None = None  # populated when ?deep=true (Phase 7.0)


class ProgramInfo(BaseModel):
    name: str
    description: str
    projects: list[ProjectInfo]


# --- Project Creation ---


class ProjectCreateRequest(BaseModel):
    slug: str = Field(..., min_length=1, max_length=63, pattern=r"^[a-z0-9][a-z0-9&+_.\-]{0,62}$")
    name: str | None = Field(None, max_length=100, description="Display name (defaults to slug)")
    program: str | None = Field(None, max_length=100)
    language: str | None = Field(None, max_length=50)
    lifecycle: ProjectLifecycle = "idea"
    description: str | None = Field(None, max_length=500)
    type: ProjectType = "work"
    scope: str | None = Field(None, max_length=50)
    owner: str | None = Field(
        None, max_length=200,
        description="Optional project-admin identity when a service caller creates on behalf of a person (RBAC F2.6)",
    )


class ProjectCreateResponse(BaseModel):
    slug: str
    name: str
    program: str | None = None
    language: str | None = None
    lifecycle: str
    description: str | None = None
    type: ProjectType
    scope: str | None = None
    metadata_path: str


class ProjectUpdateRequest(BaseModel):
    color: str | None = Field(
        ...,
        description="Nullable #rrggbb project color. Null restores the default palette.",
    )

    @field_validator("color")
    @classmethod
    def validate_color(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not _HEX_COLOR_RE.fullmatch(value):
            raise ValueError("color must be a hex string like '#rrggbb'")
        return value.lower()


# --- Status Updates ---

class StatusUpdateCreateRequest(BaseModel):
    project: str = Field(..., min_length=1, max_length=50)
    status: ProjectStatus
    what_done: str | None = Field(None, max_length=2000)
    blockers: str | None = Field(None, max_length=2000)
    next_steps: str | None = Field(None, max_length=2000)


class StatusUpdateResponse(BaseModel):
    id: int
    project: str
    status: ProjectStatus
    what_done: str | None
    blockers: str | None
    next_steps: str | None
    created_by: str
    created_at: str
    updated_at: str | None = None


# --- Feed-style Status Updates (PR #9 single-pager v2) ---

StatusUpdateKind = Literal["manual", "auto_handoff", "auto_commit", "ai_summary"]


class StatusUpdateFeedItem(BaseModel):
    """Unified feed entry — either a DB row or an on-the-fly derived entry."""
    id: str                          # int for DB rows, stringified; "handoff:<path>" / "commit:<sha>" for derived
    kind: StatusUpdateKind
    author: str                      # username / agent name / "git" for derived commits
    author_display: str | None = None
    content_md: str
    ref_id: str | None = None        # handoff path, commit sha, etc.
    created_at: str
    derived: bool = False            # true when generated on-the-fly (not persisted)


class StatusUpdateFeedResponse(BaseModel):
    updates: list[StatusUpdateFeedItem]
    total: int


class StatusUpdateFeedCreateRequest(BaseModel):
    content_md: str = Field(..., min_length=1, max_length=8000)
