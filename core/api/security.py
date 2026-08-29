# v1.7.0 - 2026-03-10 - Add resolve_session_owner for session-based MCP client attribution
from __future__ import annotations

import hashlib
import ipaddress
import json
import logging
import secrets
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import aiosqlite
import bcrypt
import jwt
from fastapi import Depends, HTTPException, Request
from fastapi.responses import Response

from core.api.config import settings
from core.api.db import get_db, get_write_db
from core.api.models import UserInfo
from core.api.models.auth import AuthMechanism
from core.api.use_cases._context import CallerContext, find_active_delegation
from core.api.use_cases.agent_tokens import (
    GRAPH_INGEST_LOCAL_TOKEN_ID_PREFIX,
    GRAPH_INGEST_SCOPE,
    GRAPH_INGEST_TOKEN_ID_PREFIX,
)

logger = logging.getLogger(__name__)

ALGORITHM = "HS256"

# Static system identities always considered valid (not in DB).
_SYSTEM_IDENTITIES: set[str] = {"marvis-local", "console-api"}

# Valid agent names for X-Agent-Name header. Used by both auth functions.
# Spoofable by anyone with the token — for attribution only, not access control.
# Initialized with generic, name-free identities; extended dynamically from the
# DB via get_valid_agent_names() and from settings.static_agent_identities
# (deploy .env). OSS core hardcodes no tenant agent names here.
_VALID_AGENT_NAMES = {"marvis-local", "console-api"}


def is_local_single_user_mode() -> bool:
    """True when local OSS mode is active and password auth is intentionally off."""
    return not settings.pir_admin_password_hash.strip()


def is_loopback_request(request: Request) -> bool:
    """Only the local machine may use passwordless local single-user mode."""
    host = request.client.host if request.client else ""
    if host in {"localhost", "testclient"}:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _local_single_user_info(request: Request) -> UserInfo:
    if not is_loopback_request(request):
        raise HTTPException(
            status_code=403,
            detail=(
                "Local single-user mode only accepts loopback requests. "
                "Bind the API to 127.0.0.1 or set MARVIS_ADMIN_PASSWORD_HASH "
                "before exposing it on a network interface."
            ),
        )
    ctx = CallerContext.local_single_user()
    return UserInfo(
        username=ctx.username,
        user_id=ctx.user_id,
        system_role=ctx.system_role,
        user_type=ctx.user_type,
        display_name="Local Operator",
        workspace_id=ctx.workspace_id,
        scopes=list(ctx.scopes),
    )


def _bind_authenticated_request(
    request: Request,
    user: UserInfo,
    *,
    auth_mechanism: AuthMechanism,
) -> UserInfo:
    """Expose only the validated principal to post-response middleware."""
    workspace_id = (user.workspace_id or "").strip()
    if not workspace_id:
        raise HTTPException(
            status_code=401,
            detail="Authenticated principal has no workspace context.",
        )
    if workspace_id != user.workspace_id:
        user = user.model_copy(update={"workspace_id": workspace_id})
    user = user.with_auth_mechanism(auth_mechanism)
    request.state.user = user
    request.state.auth_username = user.username
    request.state.auth_workspace_id = workspace_id
    return user


def _valid_static_agent_names() -> set[str]:
    """Static agent identities: hardcoded generic set ∪ configured tenant names.

    The configured names (settings.static_agent_identities) come from the deploy
    .env. Read live so tests can override via monkeypatch. This list only matters
    for the two paths that do NOT query the DB users table: the legacy shared
    TASKS_API_TOKEN attribution and a stale-cache fallback. DB-backed agents
    (e.g. seeded via migration) authenticate via get_valid_agent_names().
    """
    return _VALID_AGENT_NAMES | set(settings.static_agent_identities)


# In-memory cache for dynamic agent names from DB
_agent_names_cache: set[str] = set()
_cache_expires_at: float = 0.0
_CACHE_TTL_SECONDS: float = 300.0  # 5 minutes


async def get_valid_agent_names(db: aiosqlite.Connection) -> set[str]:
    """Return valid agent names: DB slugs union system identities. Cached for 5 minutes."""
    global _agent_names_cache, _cache_expires_at
    now = time.monotonic()
    if now < _cache_expires_at and _agent_names_cache:
        return _agent_names_cache | _SYSTEM_IDENTITIES
    try:
        async with db.execute(
            "SELECT slug FROM users WHERE type = 'agent' AND deleted_at IS NULL"
        ) as cur:
            rows = await cur.fetchall()
        _agent_names_cache = {row[0] for row in rows}
        _cache_expires_at = now + _CACHE_TTL_SECONDS
    except Exception:
        logger.warning("get_valid_agent_names: DB query failed, using stale cache")
    return _agent_names_cache | _SYSTEM_IDENTITIES


def verify_password(password: str, hashed: str) -> bool:
    """Verify password against bcrypt hash. bcrypt.checkpw is already constant-time."""
    return bcrypt.checkpw(password.encode("utf-8"), hashed.encode("utf-8"))


def create_session_jwt(
    username: str,
    extra_claims: dict | None = None,
) -> tuple[str, str, datetime]:
    """Create JWT session token. Returns (token, jti, expires_at).

    extra_claims: optional dict merged into JWT payload (e.g. workspace_id for SSO).
    """
    jti = str(uuid.uuid4())
    expires_at = datetime.now(timezone.utc) + timedelta(hours=settings.jwt_expiry_hours)
    payload = {
        "sub": username,
        "jti": jti,
        "exp": expires_at,
        "iat": datetime.now(timezone.utc),
    }
    if extra_claims:
        payload.update(extra_claims)
    token = jwt.encode(payload, settings.pir_jwt_secret, algorithm=ALGORITHM)
    return token, jti, expires_at


def verify_session_jwt(token: str) -> dict:
    """Verify and decode JWT. Raises jwt.exceptions on failure."""
    return jwt.decode(
        token,
        settings.pir_jwt_secret,
        algorithms=[ALGORITHM],
    )


async def is_token_blacklisted(jti: str, db: aiosqlite.Connection) -> bool:
    """Check if token JTI is in blacklist."""
    cursor = await db.execute("SELECT 1 FROM token_blacklist WHERE jti = ?", (jti,))
    return await cursor.fetchone() is not None


async def blacklist_token(
    jti: str, expires_at: datetime, db: aiosqlite.Connection
) -> None:
    """Add a token to the blacklist and commit the standalone logout write."""
    await persist_token_blacklist_entry(jti, expires_at, db)
    await db.commit()


async def persist_token_blacklist_entry(
    jti: str, expires_at: datetime, db: aiosqlite.Connection
) -> None:
    """Add a token inside a caller-owned transaction without committing it."""
    await db.execute(
        "INSERT OR IGNORE INTO token_blacklist (jti, expires_at) VALUES (?, ?)",
        (jti, expires_at.isoformat()),
    )


async def consume_token_proof_in_transaction(
    jti: str, expires_at: datetime, db: aiosqlite.Connection
) -> None:
    """Burn a one-use proof, failing on replay, without committing the caller."""
    await db.execute(
        "INSERT INTO token_blacklist (jti, expires_at) VALUES (?, ?)",
        (jti, expires_at.isoformat()),
    )


def set_auth_cookie(response: Response, token: str) -> None:
    """Set httpOnly session cookie."""
    response.set_cookie(
        key="pir_session",
        value=token,
        httponly=True,
        secure=True,
        samesite="lax",
        domain=settings.cookie_domain if settings.is_production else None,
        path="/",
        max_age=settings.jwt_expiry_hours * 3600,
    )


def clear_auth_cookie(response: Response) -> None:
    """Remove session cookie."""
    response.delete_cookie(
        key="pir_session",
        httponly=True,
        secure=True,
        samesite="lax",
        domain=settings.cookie_domain if settings.is_production else None,
        path="/",
    )


SIGNED_IN_COOKIE = "marvis_signed_in"


def set_signed_in_bit(response: Response) -> None:
    """Set a non-identifying "an app session exists" bit on the parent domain so
    the static marketing site can paint a signed-in header. One bit, no identity;
    the real session cookie stays HttpOnly. NOT HttpOnly, because the static site
    reads it from document.cookie on .justaskmarvis.com."""
    response.set_cookie(
        key=SIGNED_IN_COOKIE,
        value="1",
        httponly=False,
        secure=True,
        samesite="lax",
        domain=settings.cookie_domain if settings.is_production else None,
        path="/",
        max_age=settings.jwt_expiry_hours * 3600,
    )


def clear_signed_in_bit(response: Response) -> None:
    """Delete the marvis_signed_in hint on logout, mirroring set_signed_in_bit."""
    response.delete_cookie(
        key=SIGNED_IN_COOKIE,
        httponly=False,
        secure=True,
        samesite="lax",
        domain=settings.cookie_domain if settings.is_production else None,
        path="/",
    )


# In-memory WS ticket store (single-worker pir-api). Tickets are ephemeral
# (30s TTL, single-use), so they do NOT need the transactional main DB — and
# more importantly must NOT go through the shared _write_lock, whose contention
# added 8.5s p95 lock-wait on terminal open/connect (diagnostic 2026-05-27).
# A restart of pir-api drops pending tickets, which is fine: a restart also tears
# down every WebSocket, so any pending ticket would be useless anyway.
# Atomicity note: the asyncio event loop is single-threaded, so the check-then-set
# in consume (used flag) is atomic as long as there is no `await` between them.
_ws_tickets: dict[str, dict[str, Any]] = {}


@dataclass(frozen=True)
class TerminalTicketPrincipal:
    """Identity bound to a single terminal session and workspace."""

    username: str
    user_id: str
    session_name: str
    workspace_id: str


def _purge_expired_ws_tickets(now: datetime | None = None) -> None:
    """Drop expired tickets from the in-memory store (lazy GC)."""
    current = now or datetime.now(timezone.utc)
    expired = [t for t, rec in _ws_tickets.items() if current > rec["expires_at"]]
    for t in expired:
        _ws_tickets.pop(t, None)


def cleanup_expired_ws_tickets() -> int:
    """Public hook for the periodic cleanup loop. Returns count purged."""
    before = len(_ws_tickets)
    _purge_expired_ws_tickets()
    return before - len(_ws_tickets)


async def create_ws_ticket(
    username: str,
    session_name: str,
    _legacy_db: Any | None = None,
    *,
    workspace_id: str = "ws_default",
    user_id: str | None = None,
    timings: dict[str, float | str] | None = None,
) -> str:
    """Create opaque WS ticket. 30s TTL, single-use. In-memory (no DB write_lock)."""
    bound_workspace = workspace_id.strip()
    if not bound_workspace:
        raise ValueError("terminal ticket workspace_id is required")
    bound_user_id = (user_id or username).strip()
    if not bound_user_id:
        raise ValueError("terminal ticket user identity is required")
    ticket = secrets.token_urlsafe(32)
    expires_at = datetime.now(timezone.utc) + timedelta(
        seconds=settings.ws_ticket_ttl_seconds
    )
    _purge_expired_ws_tickets()
    _ws_tickets[ticket] = {
        "username": username,
        "user_id": bound_user_id,
        "session_name": session_name,
        "workspace_id": bound_workspace,
        "expires_at": expires_at,
        "used": False,
    }
    if timings is not None:
        timings["insert_ms"] = 0.0
        timings["commit_ms"] = 0.0
    return ticket


async def consume_ws_ticket(
    ticket: str,
    session_name: str,
    _legacy_db: Any | None = None,
    *,
    workspace_id: str | None = None,
    timings: dict[str, float | str] | None = None,
) -> str | None:
    """Consume WS ticket. Returns username if valid, None otherwise. Single-use."""
    principal = await consume_ws_ticket_principal(
        ticket,
        session_name,
        workspace_id=workspace_id,
        timings=timings,
    )
    return principal.username if principal is not None else None


async def consume_ws_ticket_principal(
    ticket: str,
    session_name: str,
    *,
    workspace_id: str | None = None,
    timings: dict[str, float | str] | None = None,
) -> TerminalTicketPrincipal | None:
    """Consume a ticket and return its exact user/session/workspace binding."""
    logger.info("consume_ws_ticket: session=%s", session_name)
    if timings is not None:
        timings["lookup_ms"] = 0.0

    rec = _ws_tickets.get(ticket)
    if rec is None:
        logger.warning("consume_ws_ticket: ticket NOT FOUND")
        if timings is not None:
            timings["outcome"] = "not_found"
        return None

    if rec["used"]:
        logger.warning("consume_ws_ticket: ticket already used")
        if timings is not None:
            timings["outcome"] = "used"
        return None

    if rec["session_name"] != session_name:
        logger.warning(
            "consume_ws_ticket: session mismatch: %s != %s",
            rec["session_name"],
            session_name,
        )
        if timings is not None:
            timings["outcome"] = "session_mismatch"
        return None

    if workspace_id is not None and rec["workspace_id"] != workspace_id:
        logger.warning("consume_ws_ticket: workspace mismatch")
        if timings is not None:
            timings["outcome"] = "workspace_mismatch"
        return None

    expires_at = rec["expires_at"]
    if datetime.now(timezone.utc) > expires_at:
        logger.warning("consume_ws_ticket: ticket expired at %s", expires_at)
        _ws_tickets.pop(ticket, None)
        if timings is not None:
            timings["outcome"] = "expired"
        return None

    # Mark as used — single-use. Atomic: no `await` between the check above and
    # this set on the single-threaded event loop.
    rec["used"] = True
    if timings is not None:
        timings["update_ms"] = 0.0
        timings["commit_ms"] = 0.0
        timings["outcome"] = "ok"
    return TerminalTicketPrincipal(
        username=rec["username"],
        user_id=rec["user_id"],
        session_name=rec["session_name"],
        workspace_id=rec["workspace_id"],
    )


async def get_current_user(
    request: Request, db: aiosqlite.Connection = Depends(get_db)
) -> UserInfo:
    """FastAPI dependency: extract and verify user from httpOnly cookie."""
    if is_local_single_user_mode():
        return _bind_authenticated_request(
            request,
            _local_single_user_info(request),
            auth_mechanism="local",
        )

    token = request.cookies.get("pir_session")
    if not token:
        raise HTTPException(
            status_code=401,
            detail=(
                "Not authenticated. Reason: no 'pir_session' cookie on the request. "
                "Fix: POST /api/v1/auth/login with {email, password} to set the httpOnly cookie, "
                "or call this endpoint with Authorization: Bearer <token> if you are an agent "
                "(see get_current_user_or_agent for dual-auth endpoints)."
            ),
        )

    try:
        payload = verify_session_jwt(token)
    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=401,
            detail=(
                "Token expired. Reason: the pir_session JWT exp claim is in the past "
                f"(configured TTL: {settings.jwt_expiry_hours}h). "
                "Fix: POST /api/v1/auth/login to obtain a fresh session cookie."
            ),
        )
    except jwt.InvalidTokenError:
        raise HTTPException(
            status_code=401,
            detail=(
                "Invalid token. Reason: pir_session JWT signature verification failed "
                "(wrong secret, tampered payload, or malformed token). "
                "Fix: clear the cookie and POST /api/v1/auth/login to re-authenticate. "
                "If the server was recently redeployed with a new PIR_JWT_SECRET, all old tokens are invalidated by design."
            ),
        )

    jti = payload.get("jti")
    if jti and await is_token_blacklisted(jti, db):
        raise HTTPException(
            status_code=401,
            detail=(
                "Token revoked. Reason: this token's jti is in the token_blacklist table (logout or admin revoke). "
                "Fix: POST /api/v1/auth/login to obtain a fresh session."
            ),
        )

    slug = payload.get("sub")
    if not slug:
        raise HTTPException(
            status_code=401,
            detail=(
                "Invalid token payload. Reason: decoded JWT is missing the 'sub' claim (user slug). "
                "Fix: this token was not issued by our auth flow — clear the cookie and POST /api/v1/auth/login."
            ),
        )

    # DB lookup obbligatorio -- verifica utente esiste e non cancellato
    async with db.execute(
        "SELECT id, system_role, display_name, workspace_id FROM users WHERE slug = ? AND deleted_at IS NULL",
        (slug,),
    ) as cursor:
        row = await cursor.fetchone()

    if row is None:
        raise HTTPException(
            status_code=401,
            detail=(
                f"User not found or deactivated (slug='{slug}'). "
                "Reason: JWT 'sub' claim references a users row that was deleted (deleted_at IS NOT NULL) or never existed. "
                "Fix: clear the cookie and POST /api/v1/auth/login with a valid account. "
                "If this was your account, contact an admin to restore it (UPDATE users SET deleted_at = NULL)."
            ),
        )

    # The persisted user row is authoritative.  A claim may repeat it, but it
    # may never select a different workspace and a hosted identity is never
    # silently assigned to the OSS compatibility workspace.
    ws_id = str(row["workspace_id"] or "").strip()
    if not ws_id:
        raise HTTPException(
            status_code=401,
            detail="User has no authenticated workspace assignment.",
        )
    claimed_workspace = str(payload.get("workspace_id") or "").strip()
    if claimed_workspace and claimed_workspace != ws_id:
        raise HTTPException(
            status_code=401,
            detail="Session workspace does not match the current user assignment.",
        )

    return _bind_authenticated_request(
        request,
        UserInfo(
            username=slug,
            user_id=row["id"],
            system_role=row["system_role"],
            display_name=row["display_name"],
            workspace_id=ws_id,
        ),
        auth_mechanism="session",
    )


def _hash_token(token: str) -> str:
    """Return SHA-256 hex digest of a raw bearer token."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class TokenStoreUnavailable(RuntimeError):
    """The credential registry could not prove an authentication decision."""


class TokenPrincipalInvalid(RuntimeError):
    """A token's persisted principal no longer resolves exactly."""


@dataclass(frozen=True)
class AgentTokenPrincipal:
    token_id: str
    principal_id: str
    principal_type: str
    agent_name: str
    scopes: tuple[str, ...]
    workspace_id: str
    issued_at: str | None
    expires_at: str | None
    rotation_family_id: str
    credential_kind: str
    local_runtime: bool


_GRAPH_INGEST_ONLY_SCOPES = (GRAPH_INGEST_SCOPE,)
_GRAPH_INGEST_METHOD = "POST"
_GRAPH_INGEST_PATH = "/api/v1/graph/ingest"


def _agent_token_route_allowed(
    method: str,
    path: str,
    principal: AgentTokenPrincipal,
) -> bool:
    """Whether this token class may authenticate the requested transport route."""
    if not principal.token_id.startswith(GRAPH_INGEST_TOKEN_ID_PREFIX):
        return True
    return (
        principal.scopes == _GRAPH_INGEST_ONLY_SCOPES
        and method.upper() == _GRAPH_INGEST_METHOD
        and path == _GRAPH_INGEST_PATH
    )


def _enforce_agent_token_route(
    request: Request,
    principal: AgentTokenPrincipal,
) -> None:
    """Keep the dedicated graph-ingest credential out of every other route."""
    if _agent_token_route_allowed(
        request.method,
        str(request.scope.get("path") or ""),
        principal,
    ):
        return
    raise HTTPException(
        status_code=403,
        detail=(
            f"This credential is restricted to {_GRAPH_INGEST_METHOD} "
            f"{_GRAPH_INGEST_PATH} and "
            "cannot authenticate this route."
        ),
    )


def _parse_lifecycle_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _legacy_shared_token_enabled() -> bool:
    return settings.agent_token_auth_mode == "compatibility"


async def _lookup_agent_token(
    bearer_token: str, db: aiosqlite.Connection
) -> AgentTokenPrincipal | None:
    """Resolve one live token, failing closed if its registry is unavailable."""
    token_hash = _hash_token(bearer_token)
    try:
        async with db.execute(
            "SELECT id, agent_name, token_hash, scopes, is_active, workspace_id, "
            "principal_id, principal_type, issued_at, expires_at, revoked_at, "
            "rotation_family_id, overlap_until, credential_kind "
            "FROM agent_tokens WHERE token_hash = ?",
            (token_hash,),
        ) as cursor:
            row = await cursor.fetchone()
    except Exception as exc:
        # A missing table/column, locked DB, or other lookup failure is not
        # evidence that the presented bearer belongs to the global fallback.
        raise TokenStoreUnavailable("agent token registry unavailable") from exc
    if row is None:
        return None
    if not secrets.compare_digest(str(row["token_hash"]), token_hash):
        return None
    if not bool(row["is_active"]) or row["revoked_at"]:
        return None

    now = datetime.now(timezone.utc)
    overlap_until = _parse_lifecycle_time(row["overlap_until"])
    if row["overlap_until"] is not None and (
        overlap_until is None or overlap_until <= now
    ):
        return None

    credential_kind = row["credential_kind"] or "legacy_individual"
    workspace_value = str(row["workspace_id"] or "").strip()
    principal_id = row["principal_id"] or ""
    principal_type = row["principal_type"] or ""
    issued_at = _parse_lifecycle_time(row["issued_at"])
    expires_at = _parse_lifecycle_time(row["expires_at"])
    rotation_family_id = row["rotation_family_id"] or ""

    if credential_kind == "individual":
        if (
            not workspace_value
            or not principal_id
            or principal_type not in {"human", "agent"}
            or issued_at is None
            or expires_at is None
            or not rotation_family_id
            or issued_at > now
            or expires_at <= now
        ):
            return None
    elif credential_kind == "legacy_individual":
        if settings.agent_token_auth_mode == "strict":
            return None
    else:
        return None
    # Only the explicitly temporary legacy-individual compatibility path may
    # map a pre-workspace token row to ws_default.  New individual credentials
    # above require a non-empty persisted workspace.
    workspace_id = workspace_value
    if credential_kind == "legacy_individual" and not workspace_id:
        workspace_id = "ws_default"

    scopes_raw = row["scopes"] or "[]"
    try:
        scopes = json.loads(scopes_raw)
    except (json.JSONDecodeError, TypeError):
        scopes = []
    if not isinstance(scopes, list) or any(
        not isinstance(scope, str) for scope in scopes
    ):
        scopes = []

    # Recheck the live principal on every use. Only a legacy-individual token
    # may match a pre-workspace user row; individual credentials require an
    # exact persisted workspace on both the credential and principal.
    principal_workspace_predicate = (
        "COALESCE(workspace_id, 'ws_default') = ?"
        if credential_kind == "legacy_individual"
        else "workspace_id = ?"
    )
    try:
        if principal_id:
            cursor = await db.execute(
                "SELECT id, slug, type FROM users WHERE id = ? "
                "AND deleted_at IS NULL "
                f"AND {principal_workspace_predicate}",
                (principal_id, workspace_id),
            )
        else:
            cursor = await db.execute(
                "SELECT id, slug, type FROM users WHERE slug = ? "
                "AND deleted_at IS NULL "
                f"AND {principal_workspace_predicate}",
                (row["agent_name"], workspace_id),
            )
        principal_rows = await cursor.fetchall()
    except Exception as exc:
        raise TokenStoreUnavailable("agent principal registry unavailable") from exc
    if len(principal_rows) != 1:
        return None
    principal = principal_rows[0]
    actual_type = principal["type"] or "human"
    if principal["slug"] != row["agent_name"]:
        return None
    if principal_type and actual_type != principal_type:
        return None
    if credential_kind == "individual" and principal["id"] != principal_id:
        return None

    local_runtime = row["id"].startswith(GRAPH_INGEST_LOCAL_TOKEN_ID_PREFIX)
    if local_runtime and (
        principal["id"] != "local"
        or principal["slug"] != "local"
        or workspace_id != "ws_default"
        or not row["id"].startswith(GRAPH_INGEST_TOKEN_ID_PREFIX)
    ):
        return None

    return AgentTokenPrincipal(
        token_id=row["id"],
        principal_id=principal["id"],
        principal_type=actual_type,
        agent_name=row["agent_name"],
        scopes=tuple(scopes),
        workspace_id=workspace_id,
        issued_at=row["issued_at"],
        expires_at=row["expires_at"],
        rotation_family_id=rotation_family_id or row["id"],
        credential_kind=credential_kind,
        local_runtime=local_runtime,
    )


async def _resolve_agent_userinfo(
    agent_name: str,
    db: aiosqlite.Connection,
    scopes: list[str] | None = None,
    workspace_id: str = "ws_default",
    principal_id: str | None = None,
    *,
    allow_legacy_workspace_null: bool = False,
) -> UserInfo:
    """Resolve agent_name to UserInfo via DB lookup (users table).

    Falls back to viewer role if agent not found.
    workspace_id comes from the token lookup (authoritative), not from users table.
    scopes propagated for downstream scope enforcement via require_scope().
    """
    workspace_predicate = (
        "COALESCE(workspace_id, 'ws_default') = ?"
        if allow_legacy_workspace_null
        else "workspace_id = ?"
    )
    if principal_id:
        async with db.execute(
            "SELECT id, slug, system_role, display_name, type FROM users "
            "WHERE id = ? AND slug = ? AND deleted_at IS NULL "
            f"AND {workspace_predicate}",
            (principal_id, agent_name, workspace_id),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            raise TokenPrincipalInvalid("token principal no longer resolves")
    else:
        valid_names = await get_valid_agent_names(db)
        if (
            agent_name not in valid_names
            and agent_name not in _valid_static_agent_names()
        ):
            agent_name = "agent"
        async with db.execute(
            "SELECT id, slug, system_role, display_name, type FROM users "
            "WHERE slug = ? AND deleted_at IS NULL "
            f"AND {workspace_predicate}",
            (agent_name, workspace_id),
        ) as cursor:
            row = await cursor.fetchone()

    if row is None:
        return UserInfo(
            username=f"agent:{agent_name}",
            user_id="",
            system_role="viewer",
            user_type="agent",
            display_name=agent_name,
            scopes=scopes or [],
            workspace_id=workspace_id,
        )

    return UserInfo(
        username=agent_name,
        user_id=row["id"],
        system_role=row["system_role"],
        user_type=row["type"] or "agent",
        display_name=row["display_name"],
        scopes=scopes or [],
        workspace_id=workspace_id,
    )


async def _bind_agent_token_principal(
    request: Request,
    principal: AgentTokenPrincipal,
    db: aiosqlite.Connection,
) -> UserInfo:
    """Bind verified token identity and its non-secret lifecycle reference."""
    _enforce_agent_token_route(request, principal)
    if principal.local_runtime and not is_loopback_request(request):
        raise HTTPException(
            status_code=403,
            detail="This local graph credential only accepts loopback requests.",
        )
    request.state.agent_token_id = principal.token_id
    request.state.agent_token_rotation_family_id = principal.rotation_family_id
    try:
        user = await _resolve_agent_userinfo(
            principal.agent_name,
            db,
            list(principal.scopes),
            principal.workspace_id,
            principal.principal_id,
            allow_legacy_workspace_null=(
                principal.credential_kind == "legacy_individual"
            ),
        )
    except TokenPrincipalInvalid as exc:
        raise HTTPException(
            status_code=401,
            detail="The token principal is no longer active in this workspace.",
        ) from exc
    # A dedicated graph credential is deliberately less privileged than the
    # principal that minted it.  The route gate above limits the credential to
    # graph ingest; cap its role too so an admin-backed MCP service can never
    # carry admin authority into project-access decisions.
    if principal.token_id.startswith(GRAPH_INGEST_TOKEN_ID_PREFIX):
        user = user.model_copy(update={"system_role": "operator"})
    return _bind_authenticated_request(
        request,
        user,
        auth_mechanism="local" if principal.local_runtime else "agent_token",
    )


def _token_store_http_error(exc: TokenStoreUnavailable) -> HTTPException:
    return HTTPException(
        status_code=503,
        detail="Token authentication is temporarily unavailable; retry later.",
        headers={"Retry-After": "5"},
    )


async def get_current_user_or_agent(
    request: Request, db: aiosqlite.Connection = Depends(get_db)
) -> UserInfo:
    """Dual auth: cookie (Console Marvis web) OR Bearer token (agent).

    Used for read-only endpoints accessible by both humans and agents.
    Bearer token resolution order:
    1. Per-agent token: check agent_tokens table (SHA-256 hash lookup)
    2. Legacy single token: compare against settings.tasks_api_token
    3. Fall back to pir_session cookie (existing Console Marvis auth)

    X-Agent-Name is attribution-only here — not verified against token owner.
    Strict X-Agent-Name enforcement is reserved for get_agent_user (operator endpoints).
    """
    if is_local_single_user_mode():
        return _bind_authenticated_request(
            request,
            _local_single_user_info(request),
            auth_mechanism="local",
        )

    # Path 1 & 2: Bearer token for agents
    auth_header = request.headers.get("authorization", "")
    if auth_header.startswith("Bearer "):
        bearer_token = auth_header[7:]

        # 1. Per-agent token lookup (DB)
        try:
            result = await _lookup_agent_token(bearer_token, db)
        except TokenStoreUnavailable as exc:
            raise _token_store_http_error(exc) from exc
        if result is not None:
            # X-Agent-Name is attribution-only for read endpoints — no mismatch check.
            # Strict verification is in get_agent_user (operator-level write endpoints).
            return await _bind_agent_token_principal(request, result, db)

        # 2. Legacy single shared token fallback — bound to ws_default (P1-3 review fix)
        if (
            _legacy_shared_token_enabled()
            and settings.tasks_api_token
            and secrets.compare_digest(bearer_token, settings.tasks_api_token)
        ):
            agent_name = request.headers.get("x-agent-name", "agent")
            return _bind_authenticated_request(
                request,
                await _resolve_agent_userinfo(
                    agent_name,
                    db,
                    workspace_id="ws_default",
                    allow_legacy_workspace_null=True,
                ),
                auth_mechanism="legacy_shared_token",
            )

        raise HTTPException(
            status_code=401,
            detail="Invalid or inactive API token.",
        )

    # Path 3: Cookie for Console Marvis web
    return await get_current_user(request, db)


# --- Super-session delegations (Constitution v2.0 Rule 6) ---


async def get_active_delegation(
    agent_username: str,
    workspace_id: str,
    db: aiosqlite.Connection,
) -> aiosqlite.Row | None:
    """Newest persisted, active, bounded delegation for an agent identity."""
    return await find_active_delegation(agent_username, workspace_id, db)


async def get_current_user_or_delegated_agent(
    request: Request, db: aiosqlite.Connection = Depends(get_write_db)
) -> UserInfo:
    """Human-only dependency that ALSO honors an active super-session grant.

    Resolution order:
    1. ``pir_session`` cookie -> human session, byte-identical to
       :func:`get_current_user` (the default Console path, unchanged).
    2. Bearer agent token -> the agent identity is resolved, then a live
       ``delegations`` row is REQUIRED (Constitution v2.0 Rule 6). The returned
       ``UserInfo`` keeps the AGENT identity (audit never lies about who acted)
       but carries the granter's ``system_role`` — the grant delegates
       authority, never identity. ``request.state.delegation_grant_id`` /
       ``delegation_granted_by`` are set, and the pass-through is audit-logged
       here: the one gate every human-only endpoint shares.
    3. Anything else -> the same 401 guidance as :func:`get_current_user`.
    """
    if is_local_single_user_mode():
        return _bind_authenticated_request(
            request,
            _local_single_user_info(request),
            auth_mechanism="local",
        )

    if request.cookies.get("pir_session"):
        return await get_current_user(request, db)

    auth_header = request.headers.get("authorization", "")
    if auth_header.startswith("Bearer "):
        user = await get_current_user_or_agent(request, db)
        if user.user_type != "agent":
            # A Bearer that resolves to a non-agent identity has no business on
            # a human-only endpoint without a cookie session.
            raise HTTPException(
                status_code=403,
                detail=(
                    "Human-only endpoint. Reason: Bearer token resolves to a "
                    "non-agent identity; human sessions authenticate via the "
                    "pir_session cookie. Fix: POST /api/v1/auth/login."
                ),
            )
        ws = (user.workspace_id or "").strip()
        if not ws:
            raise HTTPException(
                status_code=403,
                detail="Delegated agent has no authenticated workspace context.",
            )
        grant = await get_active_delegation(user.username, ws, db)
        if grant is None:
            raise HTTPException(
                status_code=403,
                detail=(
                    "Human-only endpoint. Reason: agent Bearer without an active "
                    "super-session delegation (Constitution v2.0 Rule 6). "
                    "Fix: a human grants one via POST /api/v1/delegations "
                    "(exchange-and-burn of a fresh human session token), or "
                    "perform this action from the Console as a human."
                ),
            )
        request.state.delegation_grant_id = grant["id"]
        request.state.delegation_granted_by = grant["granted_by"]
        from core.api.services.audit import log_audit  # local: avoid import cycle

        if not db.in_transaction:
            await db.execute("BEGIN IMMEDIATE")
        await log_audit(
            db,
            action="delegation.exercise",
            user=user.username,
            resource_type="endpoint",
            resource_id=f"{request.method} {request.url.path}",
            details={
                "delegated": True,
                "grant_id": grant["id"],
                "granted_by": grant["granted_by"],
                "effective_role": grant["granted_by_role"],
                "stage": "authorization_gate",
            },
            workspace_id=ws,
        )
        await db.commit()
        return _bind_authenticated_request(
            request,
            user.model_copy(update={"system_role": grant["granted_by_role"]}),
            auth_mechanism="delegated_agent_token",
        )

    return await get_current_user(request, db)


async def get_agent_user(
    request: Request, db: aiosqlite.Connection = Depends(get_db)
) -> UserInfo:
    """Bearer-only auth per router /agent. Nessun fallback cookie.

    Policy esplicita: solo agenti, no console web. Se il token manca o e errato → 401
    senza tentare il cookie. Questo rende la policy visibile nel codice.

    Resolution order:
    1. Per-agent token (agent_tokens table)
    2. Legacy single shared token (settings.tasks_api_token)

    For per-agent tokens (path 1): if X-Agent-Name header is present,
    it MUST match the agent_name bound to the token in DB. Returns 403 on mismatch.
    Legacy token (path 2): X-Agent-Name is attribution-only, not verified.
    """
    auth_header = request.headers.get("authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(
            status_code=401,
            detail=(
                "Bearer token required. Reason: this endpoint is agent-only (no cookie fallback); "
                "the Authorization header must be 'Bearer <token>'. "
                "Fix: add `Authorization: Bearer $TASKS_API_TOKEN` (legacy) or a per-agent token. "
                "For MCP clients, set X-Agent-Name header too so actions are correctly attributed."
            ),
        )
    bearer_token = auth_header[7:]

    # 1. Per-agent token lookup
    try:
        result = await _lookup_agent_token(bearer_token, db)
    except TokenStoreUnavailable as exc:
        raise _token_store_http_error(exc) from exc
    if result is not None:
        agent_name = result.agent_name
        # Verify X-Agent-Name header matches the token owner when present
        claimed_name = request.headers.get("x-agent-name", "").strip()
        if claimed_name and claimed_name != agent_name:
            raise HTTPException(
                status_code=403,
                detail=(
                    f"X-Agent-Name '{claimed_name}' does not match token owner '{agent_name}'. "
                    "Reason: per-agent tokens are bound to a specific agent_name in agent_tokens; "
                    "spoofing another agent's name is rejected (strict check on operator endpoints). "
                    f"Fix: either remove the X-Agent-Name header (defaults to token owner '{agent_name}'), "
                    f"or set it exactly to '{agent_name}', or use a token issued for '{claimed_name}'."
                ),
            )
        return await _bind_agent_token_principal(request, result, db)

    # 2. Legacy single shared token — bound to ws_default (P1-3 review fix).
    #    Resolve via _resolve_agent_userinfo, identical to get_current_user_or_agent:
    #    a hand-built UserInfo here defaulted user_type to "human" (breaking the
    #    delegations grant gate, which requires an agent identity) and produced an
    #    "agent:"-prefixed username that diverged from the exercise path
    #    (get_active_delegation lookup would miss). The shared resolver gives a
    #    DB-backed identity with user_type="agent" and the bare agent slug.
    if (
        _legacy_shared_token_enabled()
        and settings.tasks_api_token
        and secrets.compare_digest(bearer_token, settings.tasks_api_token)
    ):
        agent_name = request.headers.get("x-agent-name", "agent")
        return _bind_authenticated_request(
            request,
            await _resolve_agent_userinfo(
                agent_name,
                db,
                workspace_id="ws_default",
                allow_legacy_workspace_null=True,
            ),
            auth_mechanism="legacy_shared_token",
        )

    raise HTTPException(
        status_code=401,
        detail="Invalid or inactive API token.",
    )


def require_agent_token_scope(*required_scopes: str):
    """Require a per-agent token with explicit scopes.

    Unlike get_agent_user(), this rejects the legacy shared token and enforces that
    every requested scope is present on the matched agent_tokens row.
    """
    if not required_scopes:
        raise ValueError("require_agent_token_scope() requires at least one scope")

    async def check(
        request: Request,
        db: aiosqlite.Connection = Depends(get_db),
    ) -> UserInfo:
        auth_header = request.headers.get("authorization", "")
        if not auth_header.startswith("Bearer "):
            raise HTTPException(
                status_code=401,
                detail=(
                    "Bearer token required. Reason: this endpoint requires a scoped per-agent token "
                    f"(needs scopes: {list(required_scopes)}). "
                    "Fix: add `Authorization: Bearer <per-agent-token>` header — the legacy shared token "
                    "is NOT accepted here, only tokens issued via POST /api/v1/agent-tokens with the right scopes."
                ),
            )

        bearer_token = auth_header[7:]
        try:
            result = await _lookup_agent_token(bearer_token, db)
        except TokenStoreUnavailable as exc:
            raise _token_store_http_error(exc) from exc
        if result is None:
            raise HTTPException(
                status_code=401,
                detail=(
                    "Per-agent token required. Reason: the Bearer token is not present in agent_tokens "
                    f"(this endpoint needs scopes {list(required_scopes)} and the legacy shared token is not accepted). "
                    "Fix: mint a per-agent token via POST /api/v1/agent-tokens (admin) with the required scopes, "
                    "then retry."
                ),
            )

        agent_name = result.agent_name
        scopes = list(result.scopes)
        claimed_name = request.headers.get("x-agent-name", "").strip()
        if claimed_name and claimed_name != agent_name:
            raise HTTPException(
                status_code=403,
                detail=(
                    f"X-Agent-Name '{claimed_name}' does not match token owner '{agent_name}'. "
                    "Reason: per-agent tokens are bound to a specific agent_name; spoofing is rejected. "
                    f"Fix: remove X-Agent-Name header, or set it to '{agent_name}', or use a token issued for '{claimed_name}'."
                ),
            )

        missing = [scope for scope in required_scopes if scope not in scopes]
        if missing:
            raise HTTPException(
                status_code=403,
                detail=(
                    f"Token missing required scopes: {missing}. "
                    f"Reason: this endpoint needs {list(required_scopes)} but token '{agent_name}' only has {scopes}. "
                    f"Fix: mint a new token for '{agent_name}' that includes {missing} "
                    "(admin: POST /api/v1/agent-tokens with scopes=<full list>), or have an admin add the scopes."
                ),
            )

        return await _bind_agent_token_principal(request, result, db)

    return check


async def resolve_session_owner(
    request: Request, db: aiosqlite.Connection
) -> str | None:
    """Resolve owner slug from X-Session-Name header -> sessions_meta.owner_id -> users.slug.

    Used by task creation/update endpoints to attribute actions to the session's
    owner (human user) instead of the agent identity.

    Returns owner slug or None if not resolvable (no header, no owner_id set,
    or pre-migration — safe to call before migration 033 is applied).
    """
    session_name = request.headers.get("x-session-name", "").strip()
    if not session_name:
        return None
    try:
        cursor = await db.execute(
            "SELECT sm.owner_id, u.slug FROM sessions_meta sm "
            "LEFT JOIN users u ON sm.owner_id = u.id "
            "WHERE sm.name = ? AND sm.owner_id IS NOT NULL",
            (session_name,),
        )
        row = await cursor.fetchone()
        if row and row["slug"]:
            return row["slug"]
    except Exception:
        pass  # pre-migration safety: owner_id column may not exist yet
    return None


# Aliases for declarative use in routers — semantic clarity over raw function names.
# require_cookie_auth: human console sessions only (no bearer token fallback)
# require_any_auth: cookie OR bearer token (agents + humans)
require_cookie_auth = get_current_user
require_any_auth = get_current_user_or_agent
