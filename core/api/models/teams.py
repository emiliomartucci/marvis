# v2.0.0 - 2026-03-09 - Flat teams with role-based membership (member|admin)
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

TeamRole = Literal["member", "admin"]


class TeamSummary(BaseModel):
    id: str
    slug: str
    display_name: str
    role: TeamRole = "member"


class TeamResponse(BaseModel):
    id: str
    slug: str
    display_name: str
    description: str | None
    avatar_color: str | None = None
    created_at: str
    member_count: int = 0
    project_count: int = 0


class TeamCreateRequest(BaseModel):
    display_name: str = Field(..., min_length=1, max_length=100)
    slug: str | None = Field(None, max_length=50)
    description: str | None = Field(None, max_length=500)
    avatar_color: str | None = Field(None, max_length=20)


class TeamUpdateRequest(BaseModel):
    display_name: str | None = Field(None, min_length=1, max_length=100)
    description: str | None = Field(None, max_length=500)
    avatar_color: str | None = Field(None, max_length=20)


class TeamMemberRequest(BaseModel):
    user_id: str = Field(..., min_length=1, max_length=50)
    role: TeamRole = "member"


class TeamMemberResponse(BaseModel):
    user_id: str
    display_name: str
    system_role: str
    role: TeamRole
    joined_at: str


class TeamProjectAssignRequest(BaseModel):
    project: str = Field(..., min_length=1, max_length=100)
    is_public: bool = False


class TeamProjectResponse(BaseModel):
    project: str
    is_public: bool
    assigned_at: str
