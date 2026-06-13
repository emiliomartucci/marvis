# v1.0.0 - 2026-05-26 - M1 CAPTURE U1 — ingestion API-key management (mint/list/revoke)
from __future__ import annotations

import json
import logging
import re
import secrets
import uuid

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException, Query

from core.api.db import get_db, get_write_db
from core.api.models import (
    IngestKeyCreateRequest,
    IngestKeyResponse,
)
from core.api.models.ingest_keys import PROJECT_SLUG_PATTERN
from core.api.rbac import require_role
from core.api.security import _hash_token

logger = logging.getLogger(__name__)

# Mounted under the existing /api/v1/ingest namespace, beside the triage routes.
router = APIRouter(prefix="/api/v1/ingest/keys", tags=["ingest-keys"])

_TOKEN_BYTES = 32  # 256-bit raw token
_TOKEN_PREFIX = "ing_"
_DISPLAY_PREFIX_LEN = 12
_SLUG_RE = re.compile(PROJECT_SLUG_PATTERN)


def _row_to_response(
    row: aiosqlite.Row, token: str | None = None
) -> IngestKeyResponse:
    try:
        scope = json.loads(row["project_scope"] or "[]")
    except (json.JSONDecodeError, TypeError):
        scope = []
    return IngestKeyResponse(
        id=row["id"],
        name=row["name"],
        prefix=row["prefix"],
        project_scope=scope,
        ingest_policy=row["ingest_policy"],
        default_source=row["default_source"],
        rate_limit_per_min=row["rate_limit_per_min"],
        daily_quota=row["daily_quota"],
        is_active=bool(row["is_active"]),
        created_at=row["created_at"],
        created_by=row["created_by"],
        expires_at=row["expires_at"],
        last_used_at=row["last_used_at"],
        revoked_at=row["revoked_at"],
        revoke_reason=row["revoke_reason"],
        token=token,
    )


@router.post("", response_model=IngestKeyResponse, status_code=201)
async def create_ingest_key(
    body: IngestKeyCreateRequest,
    caller=Depends(require_role("admin", "super_admin", human_only=True)),
    db: aiosqlite.Connection = Depends(get_write_db),
) -> IngestKeyResponse:
    """Mint a scoped ingestion key. The raw token is returned ONCE.

    Admin+ human-only. project_scope is validated for slug FORMAT only (the real
    access boundary is enforced per-request: project ∈ key.project_scope,
    default-deny). An empty scope is rejected by the model (a key that can ingest
    nowhere is a footgun, not a feature).
    """
    invalid = [s for s in body.project_scope if not _SLUG_RE.fullmatch(s)]
    if invalid:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Invalid project slug(s) in scope: {invalid}. "
                f"Each slug must match {PROJECT_SLUG_PATTERN!r}. "
                "Fix: use lowercase project slugs (e.g. 'marvisx', 'c&i-normativa')."
            ),
        )

    raw_token = f"{_TOKEN_PREFIX}{secrets.token_urlsafe(_TOKEN_BYTES)}"
    token_hash = _hash_token(raw_token)
    key_id = f"ing_key_{uuid.uuid4().hex[:16]}"
    prefix = raw_token[:_DISPLAY_PREFIX_LEN]
    ws = getattr(caller, "workspace_id", None) or "ws_default"
    scope_json = json.dumps(sorted(set(body.project_scope)))

    try:
        await db.execute(
            """
            INSERT INTO ingest_api_keys
                (id, name, token_hash, prefix, project_scope, ingest_policy,
                 default_source, rate_limit_per_min, daily_quota, workspace_id,
                 created_by, expires_at, is_active)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
            """,
            (
                key_id,
                body.name,
                token_hash,
                prefix,
                scope_json,
                body.ingest_policy,
                body.default_source,
                body.rate_limit_per_min,
                body.daily_quota,
                ws,
                caller.username,
                body.expires_at,
            ),
        )
        await db.commit()
    except Exception as exc:
        logger.error("create_ingest_key: DB insert failed: %s", exc)
        raise HTTPException(
            status_code=500,
            detail=(
                "Failed to create ingestion key: INSERT into ingest_api_keys failed "
                "(likely a constraint violation). See server logs for details."
            ),
        )

    async with db.execute(
        "SELECT * FROM ingest_api_keys WHERE id = ?", (key_id,)
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        raise HTTPException(
            status_code=500,
            detail=f"Key {key_id!r} inserted but read-back returned no row.",
        )

    logger.info(
        "create_ingest_key: key %s (policy=%s scope=%s) minted by %s",
        key_id, body.ingest_policy, scope_json, caller.username,
    )
    return _row_to_response(row, token=raw_token)


@router.get("", response_model=list[IngestKeyResponse])
async def list_ingest_keys(
    include_revoked: bool = Query(False),
    caller=Depends(require_role("admin", "super_admin", human_only=True)),
    db: aiosqlite.Connection = Depends(get_db),
) -> list[IngestKeyResponse]:
    """List ingestion keys (prefix only, never the token). Admin+ human-only."""
    ws = getattr(caller, "workspace_id", None) or "ws_default"
    query = (
        "SELECT * FROM ingest_api_keys "
        "WHERE COALESCE(workspace_id, 'ws_default') = ? "
    )
    if not include_revoked:
        query += "AND is_active = 1 "
    query += "ORDER BY created_at DESC"
    async with db.execute(query, (ws,)) as cursor:
        rows = await cursor.fetchall()
    return [_row_to_response(r) for r in rows]


@router.delete("/{key_id}", status_code=204)
async def revoke_ingest_key(
    key_id: str,
    reason: str | None = Query(default=None, max_length=512),
    caller=Depends(require_role("admin", "super_admin", human_only=True)),
    db: aiosqlite.Connection = Depends(get_write_db),
) -> None:
    """Revoke a key (soft-delete with audit). Admin+ human-only.

    Sets is_active=0 + revoked_at/by/reason. A revoked key is rejected by
    resolve_ingest_key (401). In-flight rows already created proceed (intake was
    authorized); only new requests are rejected.
    """
    async with db.execute(
        "SELECT id, name FROM ingest_api_keys WHERE id = ?", (key_id,)
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"Ingestion key not found (key_id={key_id!r}). "
                "Fix: list keys via GET /api/v1/ingest/keys to find the correct id "
                "(this is the opaque DB id, not the token itself)."
            ),
        )

    await db.execute(
        """
        UPDATE ingest_api_keys
           SET is_active = 0,
               revoked_at = datetime('now'),
               revoked_by = ?,
               revoke_reason = ?
         WHERE id = ?
        """,
        (caller.username, reason, key_id),
    )
    await db.commit()
    logger.info(
        "revoke_ingest_key: key %s ('%s') revoked by %s (reason=%s)",
        key_id, row["name"], caller.username, reason,
    )
