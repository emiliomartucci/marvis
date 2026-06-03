# v1.0.0 - 2026-05-26 - M1 CAPTURE U1 — ingestion API-key resolution (fail-closed)
"""Resolve an ingestion API key to a typed, frozen, fail-closed context.

Security invariants (non-negotiable):
- resolve_ingest_key NEVER returns a degraded identity. Missing / invalid /
  expired / revoked / inactive → HTTPException(401). It never falls back to a
  lower-privilege role (learning 3c07f9b1 — Bearer-without-X-Agent-Name silent
  viewer regression).
- Trust (project_scope + ingest_policy) is bound to the credential, never derived
  from client payload (D9, learning 89161faf — default-deny).
- The lookup reads on the read-only pool (get_db); last_used_at is intentionally
  NOT written inline (write-amplification on the single writer, A3).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

import aiosqlite
from fastapi import Depends, HTTPException, Request

from core.api.db import get_db
from core.api.security import _hash_token

IngestPolicy = Literal["open", "trusted"]

_INVALID_KEY_DETAIL = (
    "Invalid ingestion key. Reason: the Bearer token in the Authorization header "
    "is not an active, unexpired ingestion key in ingest_api_keys. "
    "Fix: mint a key via POST /api/v1/ingest/keys (admin) and send it as "
    "`Authorization: Bearer <key>`. Ingestion keys are distinct from agent tokens; "
    "an agent/legacy token is not accepted on this endpoint."
)


@dataclass(frozen=True)
class IngestKeyContext:
    """Immutable resolved identity for an ingestion request."""

    key_id: str
    name: str
    project_scope: frozenset[str]
    ingest_policy: IngestPolicy
    default_source: str | None
    rate_limit_per_min: int
    daily_quota: int
    workspace_id: str


def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


async def resolve_ingest_key(
    bearer_token: str, db: aiosqlite.Connection
) -> IngestKeyContext:
    """Resolve a raw bearer token to an IngestKeyContext, or raise 401.

    Fail-closed: any failure mode (unknown / inactive / revoked / expired) is a
    401. Never returns Optional, never degrades.
    """
    if not bearer_token:
        raise HTTPException(status_code=401, detail=_INVALID_KEY_DETAIL)

    token_hash = _hash_token(bearer_token)
    try:
        async with db.execute(
            """
            SELECT id, name, project_scope, ingest_policy, default_source,
                   rate_limit_per_min, daily_quota, workspace_id,
                   is_active, expires_at, revoked_at
              FROM ingest_api_keys
             WHERE token_hash = ?
            """,
            (token_hash,),
        ) as cursor:
            row = await cursor.fetchone()
    except aiosqlite.Error:
        # Table missing / DB error → fail closed (never silently allow).
        raise HTTPException(status_code=401, detail=_INVALID_KEY_DETAIL)

    if row is None:
        raise HTTPException(status_code=401, detail=_INVALID_KEY_DETAIL)
    if not row["is_active"] or row["revoked_at"] is not None:
        raise HTTPException(status_code=401, detail=_INVALID_KEY_DETAIL)

    expires_at = _parse_iso(row["expires_at"])
    if expires_at is not None and datetime.now(timezone.utc) >= expires_at:
        raise HTTPException(status_code=401, detail=_INVALID_KEY_DETAIL)

    import json

    try:
        scope_list = json.loads(row["project_scope"] or "[]")
    except (json.JSONDecodeError, TypeError):
        scope_list = []
    scope = frozenset(str(s) for s in scope_list if isinstance(s, str))

    policy = row["ingest_policy"] if row["ingest_policy"] in ("open", "trusted") else "open"

    return IngestKeyContext(
        key_id=row["id"],
        name=row["name"],
        project_scope=scope,
        ingest_policy=policy,  # type: ignore[arg-type]
        default_source=row["default_source"],
        rate_limit_per_min=int(row["rate_limit_per_min"]),
        daily_quota=int(row["daily_quota"]),
        workspace_id=row["workspace_id"] or "ws_default",
    )


async def require_ingest_key(
    request: Request, db: aiosqlite.Connection = Depends(get_db)
) -> IngestKeyContext:
    """FastAPI dependency: extract Bearer and resolve, fail-closed."""
    auth_header = request.headers.get("authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(
            status_code=401,
            detail=(
                "Ingestion key required. Reason: this endpoint authenticates only via "
                "`Authorization: Bearer <ingest_api_key>` (no cookie, no degraded fallback). "
                + _INVALID_KEY_DETAIL
            ),
        )
    return await resolve_ingest_key(auth_header[7:], db)
