# v1.0.0 - 2026-05-26 - M1 CAPTURE U1/U2 — ingestion API-key + ingress contract
"""Pydantic models for the governed ingestion ingress (M1 CAPTURE).

U1 — ingest_api_keys management (mint / list / revoke).
U2 — POST /api/v1/ingest JSON payload contract.
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

IngestPolicy = Literal["open", "trusted"]

# Slug whitelist mirrors ingest_triage._PROJECT_SLUG_RE (allows `&` for legacy
# slugs like `c&i-normativa`). Format-only guard at mint time; the real access
# boundary is enforced per-request (project ∈ key.project_scope, default-deny).
PROJECT_SLUG_PATTERN = r"^[a-z0-9][a-z0-9_&\-]{0,127}$"


class IngestKeyCreateRequest(BaseModel):
    """Admin mints a scoped ingestion key. The raw token is returned ONCE."""

    name: str = Field(..., min_length=1, max_length=128)
    project_scope: list[str] = Field(
        ...,
        min_length=1,
        max_length=200,
        description="Project slugs this key may ingest into (default-deny: an empty scope is rejected).",
    )
    ingest_policy: IngestPolicy = "open"
    default_source: str | None = Field(default=None, max_length=128)
    rate_limit_per_min: int = Field(default=60, ge=1, le=10_000)
    daily_quota: int = Field(default=1000, ge=1, le=1_000_000)
    expires_at: str | None = Field(
        default=None,
        description="ISO-8601 UTC timestamp; the key is rejected after this instant. NULL = no expiry.",
    )


class IngestKeyResponse(BaseModel):
    """Key metadata. `token` is populated ONLY in the create response (shown once)."""

    id: str
    name: str
    prefix: str
    project_scope: list[str]
    ingest_policy: IngestPolicy
    default_source: str | None = None
    rate_limit_per_min: int
    daily_quota: int
    is_active: bool
    created_at: str
    created_by: str | None = None
    expires_at: str | None = None
    last_used_at: str | None = None
    revoked_at: str | None = None
    revoke_reason: str | None = None
    token: str | None = None


class IngestKeyRevokeRequest(BaseModel):
    reason: str | None = Field(default=None, max_length=512)


class IngestJsonContent(BaseModel):
    """Structured JSON content. Exactly one of text / base64 must be set.

    `url` (server-side fetch) is deferred to v1.1 — it is an SSRF pivot and
    unnecessary under the n8n-as-orchestrator model (the client already has the
    bytes). A payload carrying `url` is rejected with 422.
    """

    text: str | None = None
    base64: str | None = None
    url: str | None = None
    filename: str | None = Field(default=None, max_length=255)


class IngestJsonPayload(BaseModel):
    """application/json body for POST /api/v1/ingest."""

    project: str = Field(..., pattern=PROJECT_SLUG_PATTERN)
    source: str | None = Field(default=None, max_length=128)
    metadata: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Free-form provenance metadata. Persisted to the row and reconciled "
            "into structure_json under the `ingress_metadata` key (visible in the "
            "Triage view and the KG). Does NOT influence trust: policy and project "
            "scope are bound to the API key, never derived from the payload."
        ),
    )
    content: IngestJsonContent


class IngestIngressResponse(BaseModel):
    """Unified response for both multipart and JSON intake."""

    project: str
    source: str | None = None
    policy: IngestPolicy
    dry_run: bool = False
    would_route: str  # 'awaiting_triage' in M1 (all api_ingress lands in triage)
    queued_items: int = 0
    dedup_items: int = 0
    skipped_items: int = 0
    idempotent_replay: bool = False
