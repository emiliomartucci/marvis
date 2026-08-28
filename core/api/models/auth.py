# v1.2.0 - 2026-03-13 - Add workspace_id to UserInfo (enterprise multi-tenancy prerequisite)
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, PrivateAttr, model_validator


SystemRole = Literal["viewer", "operator", "admin", "super_admin"]
AuthMechanism = Literal[
    "unknown",
    "local",
    "session",
    "agent_token",
    "legacy_shared_token",
    "delegated_agent_token",
]
_AUTH_MECHANISMS = {
    "unknown",
    "local",
    "session",
    "agent_token",
    "legacy_shared_token",
    "delegated_agent_token",
}


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
    # Internal authorization evidence: private so it cannot widen the published
    # UserInfo response or JSON-schema contract.
    _auth_mechanism: AuthMechanism = PrivateAttr(default="unknown")

    @property
    def auth_mechanism(self) -> AuthMechanism:
        return self._auth_mechanism

    def with_auth_mechanism(self, mechanism: AuthMechanism) -> "UserInfo":
        if mechanism not in _AUTH_MECHANISMS:  # defensive for untyped callers
            raise ValueError("unknown authentication mechanism")
        bound = self.model_copy()
        bound._auth_mechanism = mechanism
        return bound


class TicketRequest(BaseModel):
    session_name: str


class TicketResponse(BaseModel):
    ticket: str


class AgentTokenCreateRequest(BaseModel):
    agent_name: str = Field(..., min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    scopes: list[str] = Field(default_factory=list, max_length=50)
    expires_in_hours: int | None = Field(default=None, ge=1)
    supersedes_id: str | None = Field(
        default=None, pattern=r"^(agt_tok_|pat_)[a-f0-9]{16}$"
    )
    overlap_minutes: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_rotation(self) -> "AgentTokenCreateRequest":
        if self.supersedes_id is None and self.overlap_minutes is not None:
            raise ValueError("overlap_minutes requires supersedes_id")
        return self


class AgentTokenResponse(BaseModel):
    """Returned after creation (includes raw token once) or for list (no token value)."""
    id: str
    agent_name: str
    scopes: list[str]
    is_active: bool
    created_at: str
    last_used_at: str | None = None
    token: str | None = None
    principal_id: str | None = None
    principal_type: str | None = None
    label: str | None = None
    issued_at: str | None = None
    expires_at: str | None = None
    revoked_at: str | None = None
    rotation_family_id: str | None = None
    supersedes_id: str | None = None
    overlap_until: str | None = None
    acknowledged_at: str | None = None
    credential_kind: str | None = None
