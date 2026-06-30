# v1.0.0 - 2026-03-03 - Shared base models and common types used across domains
# This module has NO imports from other api.models submodules to avoid circular imports.
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class HealthResponse(BaseModel):
    status: str = "ok"
    version: str = "1.0.0"


class StatusCounts(BaseModel):
    pending: int = 0
    approved: int = 0
    in_progress: int = 0
    review: int = 0
    completed: int = 0
    rejected: int = 0
    failed: int = 0


class UserSummary(BaseModel):
    """Embedded in RaciEntry and TaskResponse. Kept here to avoid circular imports."""
    id: str
    slug: str
    display_name: str
    avatar_color: str


# --- Comment types used across task and project domains ---

CommentStatus = Literal["info", "question", "blocker", "resolved"]
TargetType = Literal["program", "project", "task"]
ReactionType = Literal["+1", "-1", "eyes", "check"]


class CommentReaction(BaseModel):
    reaction: ReactionType
    created_by: str


class CommentResponse(BaseModel):
    id: int
    target_type: TargetType
    target_id: str
    body: str
    status: CommentStatus
    created_by: str
    created_at: str
    edited_at: str | None
    parent_id: int | None
    reactions: list[CommentReaction] = []
    replies: list["CommentResponse"] = []


CommentResponse.model_rebuild()


class CommentCreateRequest(BaseModel):
    target_type: TargetType
    target_id: str = Field(..., min_length=1, max_length=200)
    body: str = Field(..., min_length=1, max_length=5000)
    status: CommentStatus = "info"
    parent_id: int | None = None


class CommentUpdateRequest(BaseModel):
    body: str | None = Field(None, min_length=1, max_length=5000)
    status: CommentStatus | None = None


class ReactionCreateRequest(BaseModel):
    reaction: ReactionType
