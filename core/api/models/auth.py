# v1.2.0 - 2026-03-13 - Add workspace_id to UserInfo (enterprise multi-tenancy prerequisite)
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


SystemRole = Literal["viewer", "operator", "admin", "super_admin"]


class LoginRequest(BaseModel):
    password: str = Field(..., min_length=1)


class UserInfo(BaseModel):
    username: str
    user_id: str = ""
    system_role: str = "viewer"  # DEFAULT viewer -- least privilege
    user_type: str = "human"  # "human" or "agent" — from users.type column
    display_name: str | None = None
    workspace_id: str = "ws_default"  # tenant isolation — set from JWT or token lookup
    scopes: list[str] = Field(default_factory=list)  # from agent_tokens.scopes
    teams: list[dict[str, Any]] = Field(default_factory=list)  # TeamSummary dicts


class TicketRequest(BaseModel):
    session_name: str


class TicketResponse(BaseModel):
    ticket: str


class AgentTokenCreateRequest(BaseModel):
    agent_name: str = Field(..., min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    scopes: list[str] = Field(default_factory=list, max_length=50)


class AgentTokenResponse(BaseModel):
    """Returned after creation (includes raw token once) or for list (no token value)."""
    id: str
    agent_name: str
    scopes: list[str]
    is_active: bool
    created_at: str
    last_used_at: str | None = None
    token: str | None = None
