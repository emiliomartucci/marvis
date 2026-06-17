# v1.1.0 - 2026-06-16 - Add 7d to the grant TTL whitelist (1h/4h/24h/7d)
# v1.0.0 - 2026-06-11 - Super-session delegations (Constitution v2.0 Rule 6)
"""Super-session delegations — exchange-and-burn grant lifecycle.

Constitution v2.0 Rule 6: a human delegates their full authority to an agent
identity for a bounded time window. The grant is created by EXCHANGING a live
human session JWT (the "proof") — pasted by the human in the agent's chat —
and BURNING it on the spot (jti -> token_blacklist), so the credential left in
the transcript is inert seconds after being shared.

Invariants enforced here:
- the proof MUST be a HUMAN session JWT (an agent token or a delegation cannot
  mint further delegations — no chaining, no self-elevation);
- the grant is minted for the CALLING AGENT identity (Bearer + verified
  X-Agent-Name via ``get_agent_user``), never for a human identity;
- mandatory expiry (TTL whitelist), instant revoke;
- every create/revoke is audit-logged.

In OSS local single-user mode this surface is inert: the local caller is
already ``is_human_session=True`` by design (the four-eyes collapses locally).
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone

import aiosqlite
import jwt
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from core.api.db import get_write_db
from core.api.models import UserInfo
from core.api.security import (
    blacklist_token,
    get_agent_user,
    get_current_user,
    is_token_blacklisted,
    verify_session_jwt,
)
from core.api.services.audit import log_audit

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/delegations", tags=["delegations"])

# Whitelisted grant durations. No "forever" on purpose: a forgotten grant is an
# open door, so every grant dies on its own (Constitution Rule 6: mandatory expiry).
# 7d is the longest leash (a full work-week of autonomy) — still bounded, still
# instantly revocable, still audited.
_TTL_HOURS = {"1h": 1, "4h": 4, "24h": 24, "7d": 168}


class DelegationCreateRequest(BaseModel):
    proof_token: str = Field(..., min_length=20, description="Fresh HUMAN session JWT (burned on use)")
    ttl: str = Field("4h", description="Grant duration: 1h | 4h | 24h | 7d")


class DelegationResponse(BaseModel):
    id: str
    agent_username: str
    granted_by: str
    granted_by_role: str
    scope: str
    created_at: str
    expires_at: str
    revoked_at: str | None = None


@router.post("", response_model=DelegationResponse, status_code=201)
async def create_delegation(
    body: DelegationCreateRequest,
    agent: UserInfo = Depends(get_agent_user),
    db: aiosqlite.Connection = Depends(get_write_db),
) -> DelegationResponse:
    """Exchange a live human session JWT for a time-boxed agent grant (and burn it)."""
    if body.ttl not in _TTL_HOURS:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid ttl '{body.ttl}'. Allowed: {sorted(_TTL_HOURS)}",
        )
    if agent.user_type != "agent":
        # get_agent_user is Bearer-only, but a users-table row could be
        # misconfigured as human: a grant must land on an AGENT identity.
        raise HTTPException(
            status_code=403,
            detail="Delegations can only be granted TO an agent identity.",
        )

    # 1. Proof validation: signature + expiry.
    try:
        payload = verify_session_jwt(body.proof_token)
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Proof token expired. Paste a fresh one.")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Proof token invalid (signature/format).")

    jti = payload.get("jti")
    slug = payload.get("sub")
    if not jti or not slug:
        raise HTTPException(
            status_code=401,
            detail="Proof token missing jti/sub claims — not a session JWT from our auth flow.",
        )

    # 2. Anti-replay: a burned (or logged-out) proof is dead.
    if await is_token_blacklisted(jti, db):
        raise HTTPException(
            status_code=401,
            detail="Proof token already used or revoked (jti burned). Paste a fresh one.",
        )

    # 3. The proof must belong to a LIVE HUMAN account (never trust JWT validity
    #    alone — learning 4dcab404: deleted users keep valid tokens).
    async with db.execute(
        "SELECT id, system_role, type FROM users WHERE slug = ? AND deleted_at IS NULL",
        (slug,),
    ) as cursor:
        human = await cursor.fetchone()
    if human is None:
        raise HTTPException(status_code=401, detail="Proof token user not found or deactivated.")
    if (human["type"] or "human") != "human":
        # Invariant: no chaining — an agent's session token cannot mint grants.
        raise HTTPException(
            status_code=403,
            detail="Proof token must belong to a HUMAN account (no delegation chaining).",
        )

    # 4. Burn the proof: from this instant the pasted token is inert everywhere.
    proof_exp = datetime.fromtimestamp(payload["exp"], tz=timezone.utc)
    await blacklist_token(jti, proof_exp, db)

    # 5. Mint the grant for the calling agent identity.
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(hours=_TTL_HOURS[body.ttl])
    grant_id = str(uuid.uuid4())
    try:
        await db.execute(
            "INSERT INTO delegations "
            "(id, agent_username, granted_by, granted_by_user_id, granted_by_role, "
            " proof_jti, scope, created_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 'full', ?, ?)",
            (
                grant_id,
                agent.username,
                slug,
                human["id"],
                human["system_role"],
                jti,
                now.isoformat(),
                expires_at.isoformat(),
            ),
        )
        await db.commit()
    except aiosqlite.IntegrityError:
        # UNIQUE(proof_jti) — second line of anti-replay defense.
        raise HTTPException(status_code=409, detail="Proof token already exchanged for a grant.")

    await log_audit(
        db,
        action="delegation.create",
        user=agent.username,
        resource_type="delegation",
        resource_id=grant_id,
        details={
            "granted_by": slug,
            "granted_by_role": human["system_role"],
            "ttl": body.ttl,
            "expires_at": expires_at.isoformat(),
            "proof_jti_burned": jti,
        },
    )
    logger.info(
        "super-session grant %s: %s -> %s (role %s, ttl %s)",
        grant_id, slug, agent.username, human["system_role"], body.ttl,
    )
    return DelegationResponse(
        id=grant_id,
        agent_username=agent.username,
        granted_by=slug,
        granted_by_role=human["system_role"],
        scope="full",
        created_at=now.isoformat(),
        expires_at=expires_at.isoformat(),
    )


@router.delete("/{delegation_id}", status_code=204)
async def revoke_delegation(
    delegation_id: str,
    user: UserInfo = Depends(get_current_user),
    db: aiosqlite.Connection = Depends(get_write_db),
) -> None:
    """Revoke a grant instantly (kill switch). Human cookie session only."""
    async with db.execute(
        "SELECT id FROM delegations WHERE id = ? AND revoked_at IS NULL",
        (delegation_id,),
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Delegation not found or already revoked.")

    await db.execute(
        "UPDATE delegations SET revoked_at = ?, revoked_by = ? WHERE id = ?",
        (datetime.now(timezone.utc).isoformat(), user.username, delegation_id),
    )
    await db.commit()
    await log_audit(
        db,
        action="delegation.revoke",
        user=user.username,
        resource_type="delegation",
        resource_id=delegation_id,
        details=None,
    )


@router.get("", response_model=list[DelegationResponse])
async def list_delegations(
    user: UserInfo = Depends(get_current_user),
    db: aiosqlite.Connection = Depends(get_write_db),
) -> list[DelegationResponse]:
    """Active grants first, then the 20 most recent dead ones (digest/debug)."""
    now = datetime.now(timezone.utc).isoformat()
    async with db.execute(
        "SELECT id, agent_username, granted_by, granted_by_role, scope, "
        "       created_at, expires_at, revoked_at "
        "FROM delegations "
        "ORDER BY (revoked_at IS NULL AND expires_at > ?) DESC, created_at DESC "
        "LIMIT 50",
        (now,),
    ) as cursor:
        rows = await cursor.fetchall()
    return [
        DelegationResponse(
            id=r["id"],
            agent_username=r["agent_username"],
            granted_by=r["granted_by"],
            granted_by_role=r["granted_by_role"],
            scope=r["scope"],
            created_at=r["created_at"],
            expires_at=r["expires_at"],
            revoked_at=r["revoked_at"],
        )
        for r in rows
    ]
