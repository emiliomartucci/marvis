# v1.1.0 - 2026-03-13 - PAT self-service: users can create their own tokens
from __future__ import annotations

import hashlib
import json
import logging
import secrets
import uuid

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException

from core.api.db import get_db, get_write_db
from core.api.models import AgentTokenCreateRequest, AgentTokenResponse, UserInfo
from core.api.rbac import require_role
from core.api.security import require_any_auth

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/agent-tokens", tags=["agent-tokens"])

_TOKEN_BYTES = 32  # 256-bit raw token → urlsafe base64 ≈ 43 chars


def _hash_token(token: str) -> str:
    """Return SHA-256 hex digest of a raw bearer token."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _row_to_response(row: aiosqlite.Row, token: str | None = None) -> AgentTokenResponse:
    scopes_raw = row["scopes"] or "[]"
    try:
        scopes = json.loads(scopes_raw)
    except (json.JSONDecodeError, TypeError):
        scopes = []
    return AgentTokenResponse(
        id=row["id"],
        agent_name=row["agent_name"],
        scopes=scopes,
        is_active=bool(row["is_active"]),
        created_at=row["created_at"],
        last_used_at=row["last_used_at"],
        token=token,
    )


@router.post("", response_model=AgentTokenResponse, status_code=201)
async def create_agent_token(
    body: AgentTokenCreateRequest,
    caller=Depends(require_role("admin", "super_admin", human_only=True)),
    db: aiosqlite.Connection = Depends(get_write_db),
) -> AgentTokenResponse:
    """Create a new per-agent Bearer token.

    Returns the raw token ONCE in the response. It is never stored in plaintext
    and cannot be retrieved again. Store it securely (e.g. .env on the agent host).
    Admin+ human-only.
    """
    raw_token = secrets.token_urlsafe(_TOKEN_BYTES)
    token_hash = _hash_token(raw_token)
    token_id = f"agt_tok_{uuid.uuid4().hex[:16]}"
    scopes_json = json.dumps(body.scopes)

    try:
        ws = caller.workspace_id or "ws_default"
        await db.execute(
            "INSERT INTO agent_tokens (id, agent_name, token_hash, scopes, is_active, workspace_id) "
            "VALUES (?, ?, ?, ?, 1, ?)",
            (token_id, body.agent_name, token_hash, scopes_json, ws),
        )
        await db.commit()
    except Exception as exc:
        logger.error("create_agent_token: DB insert failed: %s", exc)
        raise HTTPException(
            status_code=500,
            detail=(
                "Failed to create token: DB INSERT into agent_tokens failed. "
                "Reason: likely a constraint violation (duplicate token_id, missing workspace_id, "
                f"or agent_name unknown in users table). See server logs for exact exception: {exc!r}. "
                "Fix: retry (new token_id each time), or check that agent_name exists and workspace_id is valid."
            ),
        )

    async with db.execute(
        "SELECT id, agent_name, scopes, is_active, created_at, last_used_at "
        "FROM agent_tokens WHERE id = ?",
        (token_id,),
    ) as cursor:
        row = await cursor.fetchone()

    if row is None:
        raise HTTPException(
            status_code=500,
            detail=(
                f"Token {token_id!r} was inserted but the read-back query returned no row. "
                "Reason: likely a transaction/replication race (write committed to a different DB than the read pool), "
                "or the token was deleted between INSERT and SELECT. "
                "Fix: retry the create call; the original token id may be unusable. Check api/db.py for write/read DB pool config."
            ),
        )

    logger.info(
        "create_agent_token: token %s created for agent '%s' by %s",
        token_id, body.agent_name, caller.username,
    )
    return _row_to_response(row, token=raw_token)


@router.get("", response_model=list[AgentTokenResponse])
async def list_agent_tokens(
    caller=Depends(require_role("admin", "super_admin", human_only=True)),
    db: aiosqlite.Connection = Depends(get_db),
) -> list[AgentTokenResponse]:
    """List all active agent tokens. Token values are NOT included.

    Admin+ human-only.
    """
    ws = caller.workspace_id or "ws_default"
    async with db.execute(
        "SELECT id, agent_name, scopes, is_active, created_at, last_used_at "
        "FROM agent_tokens WHERE is_active = 1 AND COALESCE(workspace_id, 'ws_default') = ? "
        "ORDER BY created_at DESC",
        [ws],
    ) as cursor:
        rows = await cursor.fetchall()
    return [_row_to_response(r) for r in rows]


@router.delete("/{token_id}", status_code=204)
async def revoke_agent_token(
    token_id: str,
    caller=Depends(require_role("admin", "super_admin", human_only=True)),
    db: aiosqlite.Connection = Depends(get_write_db),
) -> None:
    """Revoke (soft-delete) a per-agent token by ID.

    Sets is_active = 0. The token will no longer be accepted for authentication.
    Admin+ human-only.
    """
    async with db.execute(
        "SELECT id, agent_name FROM agent_tokens WHERE id = ?", (token_id,)
    ) as cursor:
        row = await cursor.fetchone()

    if row is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"Agent token not found (token_id={token_id!r}). "
                "Reason: no row in agent_tokens with this id (may be already revoked or wrong id). "
                "Fix: list active tokens via GET /api/v1/agent-tokens to find the correct id; "
                "note token_id is NOT the token itself — it's the opaque DB id shown in the list response."
            ),
        )

    await db.execute(
        "UPDATE agent_tokens SET is_active = 0 WHERE id = ?", (token_id,)
    )
    await db.commit()

    logger.info(
        "revoke_agent_token: token %s (agent '%s') revoked by %s",
        token_id, row["agent_name"], caller.username,
    )


# --- PAT Self-Service (any authenticated user) ---


@router.post("/personal", response_model=AgentTokenResponse, status_code=201)
async def create_personal_token(
    body: AgentTokenCreateRequest,
    user: UserInfo = Depends(require_any_auth),
    db: aiosqlite.Connection = Depends(get_write_db),
) -> AgentTokenResponse:
    """Create a Personal Access Token (PAT) for the current user.

    Unlike admin token creation, this binds the token to the calling user's slug.
    The agent_name in the body is used as a label, but the token is always
    owned by and authenticated as the current user.

    Returns the raw token ONCE. Store it securely.
    """
    raw_token = secrets.token_urlsafe(_TOKEN_BYTES)
    token_hash = _hash_token(raw_token)
    token_id = f"pat_{uuid.uuid4().hex[:16]}"
    scopes_json = json.dumps(body.scopes)
    ws = user.workspace_id or "ws_default"

    # Use user's slug as agent_name (token owner), body.agent_name as label
    label = body.agent_name or user.username

    try:
        await db.execute(
            "INSERT INTO agent_tokens (id, agent_name, token_hash, scopes, is_active, workspace_id) "
            "VALUES (?, ?, ?, ?, 1, ?)",
            (token_id, label, token_hash, scopes_json, ws),
        )
        await db.commit()
    except Exception as exc:
        logger.error("create_personal_token: DB insert failed: %s", exc)
        raise HTTPException(
            status_code=500,
            detail=(
                "Failed to create token: DB INSERT into agent_tokens failed. "
                "Reason: likely a constraint violation (duplicate token_id, missing workspace_id, "
                f"or agent_name unknown in users table). See server logs for exact exception: {exc!r}. "
                "Fix: retry (new token_id each time), or check that agent_name exists and workspace_id is valid."
            ),
        )

    async with db.execute(
        "SELECT id, agent_name, scopes, is_active, created_at, last_used_at "
        "FROM agent_tokens WHERE id = ?",
        (token_id,),
    ) as cursor:
        row = await cursor.fetchone()

    if row is None:
        raise HTTPException(
            status_code=500,
            detail=(
                f"Token {token_id!r} was inserted but the read-back query returned no row. "
                "Reason: likely a transaction/replication race (write committed to a different DB than the read pool), "
                "or the token was deleted between INSERT and SELECT. "
                "Fix: retry the create call; the original token id may be unusable. Check api/db.py for write/read DB pool config."
            ),
        )

    logger.info("create_personal_token: PAT %s created by %s (label=%s)", token_id, user.username, label)
    return _row_to_response(row, token=raw_token)


@router.get("/personal", response_model=list[AgentTokenResponse])
async def list_personal_tokens(
    user: UserInfo = Depends(require_any_auth),
    db: aiosqlite.Connection = Depends(get_db),
) -> list[AgentTokenResponse]:
    """List current user's active tokens. Token values are NOT included."""
    ws = user.workspace_id or "ws_default"
    async with db.execute(
        "SELECT id, agent_name, scopes, is_active, created_at, last_used_at "
        "FROM agent_tokens WHERE agent_name = ? AND is_active = 1 "
        "AND COALESCE(workspace_id, 'ws_default') = ? ORDER BY created_at DESC",
        (user.username, ws),
    ) as cursor:
        rows = await cursor.fetchall()
    return [_row_to_response(r) for r in rows]


@router.delete("/personal/{token_id}", status_code=204)
async def revoke_personal_token(
    token_id: str,
    user: UserInfo = Depends(require_any_auth),
    db: aiosqlite.Connection = Depends(get_write_db),
) -> None:
    """Revoke a personal token. Users can only revoke their own tokens."""
    async with db.execute(
        "SELECT id, agent_name FROM agent_tokens WHERE id = ? AND agent_name = ?",
        (token_id, user.username),
    ) as cursor:
        row = await cursor.fetchone()

    if row is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"Personal token not found or not owned by you (token_id={token_id!r}, user={user.username!r}). "
                "Reason: the token id doesn't exist, is already revoked, OR belongs to a different user. "
                "Fix: list your tokens via GET /api/v1/agent-tokens/personal to find the correct id. "
                "Admins can revoke other users' tokens via DELETE /api/v1/agent-tokens/{token_id} instead."
            ),
        )

    await db.execute("UPDATE agent_tokens SET is_active = 0 WHERE id = ?", (token_id,))
    await db.commit()
    logger.info("revoke_personal_token: PAT %s revoked by %s", token_id, user.username)
