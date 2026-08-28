# v2.3.0 - 2026-04-14 - Single-writer: login uses get_write_db (batch 5/6)
from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import time
from collections import defaultdict
from datetime import datetime, timezone

import aiosqlite
import bcrypt
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from pydantic import BaseModel, EmailStr, Field

from core.api.client_identity import resolve_client_ip
from core.api.config import settings
from core.api.db import get_db, get_write_db
from core.api.models import (
    HealthResponse,
    TicketRequest,
    TicketResponse,
    UserInfo,
)
from core.api.rbac import require_role
from core.api.use_cases._roles import SSO_ROLE_MAPPING as _SSO_ROLE_MAPPING
from core.api.security import (
    blacklist_token,
    clear_auth_cookie,
    clear_signed_in_bit,
    create_session_jwt,
    create_ws_ticket,
    get_current_user,
    set_auth_cookie,
    set_signed_in_bit,
    verify_session_jwt,
)
from core.api.services.terminal_metrics import TerminalMetricsCollector
from dataclasses import dataclass
import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from core.api.use_cases._roles import map_sso_role
import hashlib
import sqlite3
from uuid import uuid4
from fastapi.responses import Response

logger = logging.getLogger(__name__)

router = APIRouter()

# In-memory rate limiting
_login_attempts: dict[str, list[float]] = defaultdict(list)
MAX_LOGIN_ATTEMPTS = 5
LOGIN_WINDOW_SECONDS = 300

# Pre-computed dummy hash for constant-time comparison when user not found
DUMMY_HASH: bytes = bcrypt.hashpw(b"dummy-placeholder", bcrypt.gensalt())


async def _users_column_exists(db: aiosqlite.Connection, column_name: str) -> bool:
    async with db.execute("PRAGMA table_info(users)") as cursor:
        rows = await cursor.fetchall()
    return any(row["name"] == column_name for row in rows)


async def _password_must_change(db: aiosqlite.Connection, user_id: str) -> bool:
    if not settings.force_password_change_on_first_login:
        return False
    if not await _users_column_exists(db, "password_must_change"):
        return False
    async with db.execute(
        "SELECT password_must_change FROM users WHERE id = ?",
        [user_id],
    ) as cursor:
        row = await cursor.fetchone()
    return bool(row and row["password_must_change"])


async def _clear_password_must_change(
    db: aiosqlite.Connection,
    user_id: str,
    *,
    workspace_id: str | None = None,
) -> None:
    if not settings.force_password_change_on_first_login:
        return
    if not await _users_column_exists(db, "password_must_change"):
        return
    if workspace_id is None:
        await db.execute(
            "UPDATE users SET password_must_change = 0, "
            "updated_at = datetime('now') WHERE id = ?",
            [user_id],
        )
    else:
        await db.execute(
            "UPDATE users SET password_must_change = 0, "
            "updated_at = datetime('now') WHERE id = ? AND workspace_id = ?",
            [user_id, workspace_id],
        )


def _terminal_metrics_collector(
    request: Request, workspace_id: str
) -> TerminalMetricsCollector | None:
    if workspace_id == "ws_default":
        collector = getattr(request.app.state, "terminal_metrics", None)
        if not isinstance(collector, TerminalMetricsCollector):
            collector = TerminalMetricsCollector()
            request.app.state.terminal_metrics = collector
        return collector
    collectors = getattr(request.app.state, "terminal_metrics_by_workspace", None)
    if not isinstance(collectors, dict):
        collectors = {}
        request.app.state.terminal_metrics_by_workspace = collectors
    collector = collectors.get(workspace_id)
    if not isinstance(collector, TerminalMetricsCollector):
        collector = TerminalMetricsCollector()
        collectors[workspace_id] = collector
    return collector


def _get_reset_serializer() -> URLSafeTimedSerializer:
    """Return itsdangerous serializer for password reset tokens."""
    return URLSafeTimedSerializer(settings.pir_jwt_secret, salt="password-reset")


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(..., min_length=1, max_length=72)


class IssueResetTokenRequest(BaseModel):
    user_id: str = Field(..., min_length=1, max_length=50)


class ResetPasswordRequest(BaseModel):
    token: str = Field(..., min_length=1)
    new_password: str = Field(..., min_length=8, max_length=72)


def _check_rate_limit(client_ip: str) -> None:
    """Rate limit login attempts: max 5 per 5 minutes per IP."""
    now = time.time()
    attempts = _login_attempts[client_ip]
    _login_attempts[client_ip] = [t for t in attempts if now - t < LOGIN_WINDOW_SECONDS]
    if len(_login_attempts[client_ip]) >= MAX_LOGIN_ATTEMPTS:
        raise HTTPException(status_code=429, detail="Too many login attempts")


@router.get("/health", response_model=HealthResponse)
async def health():
    """Health check endpoint. No auth required."""
    return HealthResponse()


@router.post("/auth/login")
async def login(
    body: LoginRequest,
    request: Request,
    db: aiosqlite.Connection = Depends(get_write_db),
):
    """Authenticate by email + password and set httpOnly session cookie."""
    client_ip = resolve_client_ip(
        peer_ip=request.client.host if request.client else None,
        # nginx/Caddy overwrite the internal header. A host-installed
        # cloudflared process connects directly over loopback and supplies the
        # Cloudflare header instead. In both cases the shared resolver accepts
        # it only when the socket peer is an exact trusted host route.
        # Prefer Cloudflare's edge-overwritten header for the direct host
        # tunnel path. nginx/Caddy strip that header before setting the
        # internal one, so an external caller cannot choose which wins.
        forwarded_ip=(
            request.headers.get("CF-Connecting-IP")
            or request.headers.get("X-Marvis-Client-IP")
        ),
        trusted_proxy_cidrs=settings.trusted_proxy_cidrs,
    )
    _check_rate_limit(client_ip)

    # An email is not a workspace selector.  Fetch at most two matches so a
    # multi-workspace duplicate fails closed instead of authenticating whichever
    # row SQLite happened to return first.
    async with db.execute(
        "SELECT id, slug, password_hash, workspace_id FROM users "
        "WHERE lower(email) = lower(?) AND type = 'human' AND deleted_at IS NULL "
        "LIMIT 2",
        [body.email],
    ) as cursor:
        rows = await cursor.fetchall()
    row = rows[0] if len(rows) == 1 else None

    if row is None or not row["password_hash"]:
        # Dummy bcrypt to prevent timing oracle
        bcrypt.checkpw(body.password.encode("utf-8"), DUMMY_HASH)
        _login_attempts[client_ip].append(time.time())
        logger.warning("Failed login attempt from %s (email: %s)", client_ip, body.email)
        raise HTTPException(status_code=401, detail="Invalid credentials")

    if not bcrypt.checkpw(body.password.encode("utf-8"), row["password_hash"].encode("utf-8")):
        _login_attempts[client_ip].append(time.time())
        logger.warning("Failed login attempt from %s (email: %s)", client_ip, body.email)
        raise HTTPException(status_code=401, detail="Invalid credentials")

    workspace_id = str(row["workspace_id"] or "").strip()
    if not workspace_id:
        raise HTTPException(status_code=403, detail="Account has no workspace assignment")

    # A hosted AuthKit workspace must not retain a parallel password path.
    # Check only after password verification to avoid turning the response into
    # an account-enumeration oracle.
    if settings.sso_enabled and _workos_authkit_enabled():
        async with db.execute(
            "SELECT 1 FROM oidc_providers "
            "WHERE workspace_id = ? AND enabled = 1 LIMIT 1",
            (workspace_id,),
        ) as cursor:
            authkit_required = await cursor.fetchone() is not None
        if authkit_required:
            raise HTTPException(
                status_code=403,
                detail="Password login disabled for this workspace",
            )

    if await _password_must_change(db, row["id"]):
        serializer = _get_reset_serializer()
        token = serializer.dumps(
            {
                "user_id": row["id"],
                "slug": row["slug"],
                "workspace_id": workspace_id,
            }
        )
        return JSONResponse(
            status_code=403,
            content={
                "status": "password_change_required",
                "reset_token": token,
                "user_id": row["id"],
            },
        )

    slug = row["slug"]
    token, _jti, _expires_at = create_session_jwt(
        slug,
        extra_claims={"workspace_id": workspace_id},
    )
    response = JSONResponse(content={"status": "ok"})
    set_auth_cookie(response, token)
    set_signed_in_bit(response)
    logger.info("Successful login from %s (user: %s)", client_ip, slug)

    # Log console access event for security monitoring.
    # Fire-and-forget: save_events_to_db acquires _write_lock via write_db(),
    # which would DEADLOCK inside this handler (login already holds _write_lock
    # via Depends(get_write_db) — asyncio.Lock is not reentrant).
    try:
        from core.api.services.security_collector import security_collector
        import time as _time
        asyncio.create_task(security_collector.save_events_to_db([{
            "timestamp": int(_time.time()),
            "event_type": "console_login",
            "source_ip": client_ip,
            "username": slug,
            "details": {"user_agent": request.headers.get("user-agent", "")[:200]},
        }]))
    except Exception:
        pass  # Non-critical: never block login on monitoring failure

    return response


@router.get("/auth/me")
async def me(
    current_user: UserInfo = Depends(get_current_user),
    db: aiosqlite.Connection = Depends(get_db),
):
    """Return current user info including teams."""
    # Fetch teams for this user
    async with db.execute(
        "SELECT t.id, t.slug, t.display_name, tm.role "
        "FROM teams t JOIN team_members tm ON t.id = tm.team_id "
        "WHERE tm.user_id = ? AND t.deleted_at IS NULL",
        [current_user.user_id],
    ) as cursor:
        team_rows = await cursor.fetchall()

    teams = [
        {
            "id": r["id"],
            "slug": r["slug"],
            "display_name": r["display_name"],
            "role": r["role"] or "member",
        }
        for r in team_rows
    ]

    # gh #22 — surface backend capabilities the console needs to stay honest
    # about degraded modes (e.g. todos classification falling back to the
    # heuristic because the gateway LLM key is missing). Function-local import
    # keeps auth.py free of the todos service graph at module load.
    from core.api.services.todos.llm.factory import todos_llm_key_missing

    workspace_id = str(getattr(current_user, "workspace_id", None) or "").strip()
    if not workspace_id:
        raise HTTPException(401, "Authenticated workspace context is required")
    return {
        "username": current_user.username,
        "user_id": current_user.user_id,
        "system_role": current_user.system_role,
        "display_name": current_user.display_name,
        "teams": teams,
        "capabilities": {
            "todos_llm_key_missing": await todos_llm_key_missing(db, workspace_id),
        },
    }


def _sid_from_access_token(access_token: str | None) -> str | None:
    """Read the WorkOS session id (`sid`) from the access-token JWT WITHOUT
    verifying its signature — we only need the opaque id and it arrived over TLS
    from WorkOS in our own backend call. Best-effort: never raises."""
    if not access_token or not isinstance(access_token, str):
        return None
    try:
        claims = jwt.decode(access_token, options={"verify_signature": False})
    except Exception:  # noqa: BLE001 - a malformed token just means no sid
        return None
    sid = claims.get("sid")
    return sid if isinstance(sid, str) and sid else None


def _post_logout_return_to() -> str | None:
    """Where WorkOS sends the browser after ending the AuthKit session. Must be an
    allowed logout redirect in the WorkOS dashboard; unset => WorkOS default."""
    url = os.environ.get("MARVIS_POST_LOGOUT_URL", "").strip()
    return url or None


def _workos_logout_url(wos_session_id: str | None) -> str | None:
    """URL that ENDS the WorkOS AuthKit session in the browser. Clearing our own
    cookie is not logout (OWASP Session Management): WorkOS would otherwise
    silently re-authenticate. Best-effort; returns None if unavailable."""
    if not wos_session_id or not _workos_authkit_enabled():
        return None
    if not settings.workos_api_key or not settings.workos_client_id:
        return None
    try:
        import workos

        client = workos.WorkOSClient(
            api_key=settings.workos_api_key,
            client_id=settings.workos_client_id,
        )
        return client.user_management.get_logout_url(
            session_id=wos_session_id,
            return_to=_post_logout_return_to(),
        )
    except Exception as exc:  # noqa: BLE001 - never fail logout on this
        logger.warning("WorkOS logout URL unavailable: %s", exc)
        return None


def _clear_session_cookies(response: Response) -> None:
    """Clear every session artifact on both surfaces: the app JWT (host-only AND
    the legacy parent-domain variant) plus the non-identifying marvis_signed_in
    bit the static site reads."""
    clear_auth_cookie(response)
    if settings.is_production and settings.cookie_domain:
        # AuthKit sessions are deliberately host-only, while legacy sessions may
        # still use the configured parent domain. Delete both variants.
        response.delete_cookie(
            key="pir_session",
            httponly=True,
            secure=True,
            samesite="lax",
            path="/",
        )
    clear_signed_in_bit(response)


async def _perform_logout(
    request: Request, db: aiosqlite.Connection, *, redirect: bool
):
    """Blacklist the app token, END the WorkOS AuthKit session, and clear both
    surfaces. GET redirects the browser (the marketing 'Esci' link is a GET);
    POST returns the logout_url so the Console can redirect the browser itself."""
    token = request.cookies.get("pir_session")
    wos_session_id: str | None = None
    if token:
        try:
            payload = verify_session_jwt(token)
            wos_session_id = payload.get("wos_sid")
            jti = payload.get("jti")
            if jti:
                expires_at = datetime.fromtimestamp(payload["exp"], tz=timezone.utc)
                await blacklist_token(jti, expires_at, db)
        except Exception:
            pass  # Token already invalid; still clear cookies below.

    logout_url = _workos_logout_url(wos_session_id)
    if redirect:
        response: Response = RedirectResponse(
            url=logout_url or _post_logout_return_to() or "/",
            status_code=302,
        )
    else:
        response = JSONResponse(content={"status": "ok", "logout_url": logout_url})
    _clear_session_cookies(response)
    return response


@router.get("/auth/logout")
async def logout_redirect(
    request: Request,
    db: aiosqlite.Connection = Depends(get_write_db),
):
    """Browser logout: the static-site 'Esci' link is a plain GET. Ends the WorkOS
    session and clears both surfaces, then redirects."""
    return await _perform_logout(request, db, redirect=True)


@router.post("/auth/logout")
async def logout(
    request: Request,
    db: aiosqlite.Connection = Depends(get_write_db),
):
    """Programmatic logout (Console fetch). Returns logout_url for the client to
    send the browser to, and clears both surfaces."""
    return await _perform_logout(request, db, redirect=False)


@router.post("/auth/admin/issue-reset-token")
async def issue_reset_token(
    body: IssueResetTokenRequest,
    caller: UserInfo = Depends(require_role("admin", "super_admin", human_only=True)),
    db: aiosqlite.Connection = Depends(get_write_db),
):
    """Issue a password-reset token for a human user. Admin+ only."""
    workspace_id = (caller.workspace_id or "").strip()
    if not workspace_id:
        raise HTTPException(status_code=403, detail="Workspace identity required")
    async with db.execute(
        "SELECT id, slug, password_hash, type FROM users "
        "WHERE id = ? AND workspace_id = ? AND deleted_at IS NULL",
        [body.user_id, workspace_id],
    ) as cursor:
        row = await cursor.fetchone()

    if row is None:
        raise HTTPException(status_code=404, detail="User not found")
    if row["type"] != "human":
        raise HTTPException(status_code=400, detail="Reset tokens only supported for human users")

    # Guard: only allow resetting if password_hash is null (fresh user) or caller is super_admin
    if row["password_hash"] is not None and caller.system_role != "super_admin":
        raise HTTPException(
            status_code=403,
            detail="Only super_admin can re-issue tokens for users with an existing password",
        )

    serializer = _get_reset_serializer()
    token = serializer.dumps(
        {
            "user_id": row["id"],
            "slug": row["slug"],
            "workspace_id": workspace_id,
        }
    )

    logger.info("Reset token issued for user %s by %s", row["slug"], caller.username)
    return {"token": token, "user_id": row["id"], "slug": row["slug"]}


@router.post("/auth/reset-password")
async def reset_password(
    body: ResetPasswordRequest,
    db: aiosqlite.Connection = Depends(get_write_db),
):
    """Reset password using a time-limited reset token. Token expires in 24h."""
    serializer = _get_reset_serializer()

    # Step 1: decode + verify token (max age 86400 = 24h)
    try:
        data = serializer.loads(body.token, max_age=86400)
    except SignatureExpired:
        raise HTTPException(status_code=400, detail="Reset token expired")
    except BadSignature:
        raise HTTPException(status_code=400, detail="Invalid reset token")

    user_id = data.get("user_id")
    workspace_id = data.get("workspace_id")
    if (
        not isinstance(user_id, str)
        or not user_id.strip()
        or not isinstance(workspace_id, str)
        or not workspace_id.strip()
    ):
        raise HTTPException(status_code=400, detail="Invalid reset token payload")
    user_id = user_id.strip()
    workspace_id = workspace_id.strip()

    # Step 2: verify user still exists
    async with db.execute(
        "SELECT id, slug FROM users "
        "WHERE id = ? AND workspace_id = ? AND deleted_at IS NULL",
        [user_id, workspace_id],
    ) as cursor:
        row = await cursor.fetchone()

    if row is None:
        raise HTTPException(status_code=404, detail="User not found")

    # Step 3: hash new password and update
    new_hash = bcrypt.hashpw(body.new_password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
    now = datetime.now(timezone.utc).isoformat()
    await db.execute(
        "UPDATE users SET password_hash = ?, updated_at = ? "
        "WHERE id = ? AND workspace_id = ?",
        [new_hash, now, user_id, workspace_id],
    )
    await _clear_password_must_change(
        db,
        user_id,
        workspace_id=workspace_id,
    )
    await db.commit()

    logger.info("Password reset completed for user %s", row["slug"])
    return {"status": "ok", "slug": row["slug"]}


@router.post("/terminal/ticket", response_model=TicketResponse)
async def get_terminal_ticket(
    body: TicketRequest,
    request: Request,
    user: UserInfo = Depends(require_role("operator", "admin", "super_admin", human_only=True)),
    db: aiosqlite.Connection = Depends(get_db),
):
    """Generate opaque WS ticket for terminal connection. Operator+ human-only."""
    workspace_id = (user.workspace_id or "").strip()
    if not workspace_id:
        raise HTTPException(status_code=403, detail="Terminal session not found")

    async with db.execute("PRAGMA table_info(sessions_meta)") as cursor:
        session_columns = {row["name"] for row in await cursor.fetchall()}
    if "workspace_id" in session_columns:
        row = await (
            await db.execute(
                "SELECT 1 FROM sessions_meta WHERE name = ? AND workspace_id = ?",
                (body.session_name, workspace_id),
            )
        ).fetchone()
    elif settings.deploy_mode == "core" and not settings.multi_tenant_enabled:
        row = await (
            await db.execute(
                "SELECT 1 FROM sessions_meta WHERE name = ?", (body.session_name,)
            )
        ).fetchone()
    else:
        raise HTTPException(status_code=503, detail="Terminal session unavailable")
    if row is None:
        raise HTTPException(status_code=404, detail="Terminal session not found")

    collector = _terminal_metrics_collector(request, workspace_id)
    started = time.perf_counter()
    lock_wait_ms: float | None = None
    timings: dict[str, float | str] = {}
    try:
        # In-memory ticket store — no write_lock, no DB. lock_wait stays ~0.
        lock_started = time.perf_counter()
        ticket = await create_ws_ticket(
            user.username,
            body.session_name,
            workspace_id=workspace_id,
            user_id=user.user_id or user.username,
            timings=timings,
        )
        lock_wait_ms = (time.perf_counter() - lock_started) * 1000
    except Exception:
        if collector:
            metadata = dict(timings)
            if lock_wait_ms is not None:
                metadata["lock_wait_ms"] = lock_wait_ms
            collector.record_terminal_ticket_event(
                kind="issue",
                session_name=body.session_name,
                duration_ms=(time.perf_counter() - started) * 1000,
                outcome="error",
                metadata=metadata,
            )
        raise

    if collector:
        metadata = dict(timings)
        if lock_wait_ms is not None:
            metadata["lock_wait_ms"] = lock_wait_ms
        collector.record_terminal_ticket_event(
            kind="issue",
            session_name=body.session_name,
            duration_ms=(time.perf_counter() - started) * 1000,
            outcome="ok",
            metadata=metadata,
        )
    return TicketResponse(ticket=ticket)


# ---------------------------------------------------------------------------
# SSO / OIDC (WorkOS AuthKit)
# ---------------------------------------------------------------------------

# WorkOS role → Marvis system_role mapping lives in use_cases._roles (single
# source shared with the MCP OAuth context, which cannot import this router).




def _workos_authkit_enabled() -> bool:
    return bool(os.environ.get("WORKOS_AUTHKIT_DOMAIN", "").strip())


def _workos_membership_role_slug(membership: object) -> str | None:
    role = getattr(membership, "role", None)
    if isinstance(role, dict):
        slug = role.get("slug")
        return slug if isinstance(slug, str) and slug.strip() else None
    slug = getattr(role, "slug", None)
    return slug if isinstance(slug, str) and slug.strip() else None


def _workos_cookie_domain() -> str | None:
    """Keep hosted AuthKit state and sessions bound to the exact tenant host."""
    return None if _workos_authkit_enabled() else settings.cookie_domain


def _require_authkit_org(auth_response: object, configured_org_id: str) -> str:
    if not configured_org_id:
        raise HTTPException(503, "SSO organization not configured")
    auth_org_id = getattr(auth_response, "organization_id", None)
    if not isinstance(auth_org_id, str) or not auth_org_id:
        raise HTTPException(403, "SSO organization missing")
    if auth_org_id != configured_org_id:
        raise HTTPException(403, "SSO organization mismatch")
    return auth_org_id


def _require_active_membership_role(memberships: object) -> str:
    for membership in getattr(memberships, "data", []) or []:
        if getattr(membership, "status", None) != "active":
            continue
        role_slug = _workos_membership_role_slug(membership)
        if role_slug:
            return role_slug
    raise HTTPException(403, "Active organization membership required")


@router.get("/auth/sso/login")
async def sso_login(
    workspace: str,
    request: Request,
    db: aiosqlite.Connection = Depends(get_db),
):
    """Redirect to WorkOS AuthKit for SSO login.

    CSRF protection: random state token stored in HttpOnly cookie (max_age=600).
    redirect_uri is HARDCODED — never accepted from client (prevents open redirect).
    """
    if not settings.sso_enabled:
        raise HTTPException(404, "SSO not enabled")
    if not settings.workos_api_key or not settings.workos_client_id:
        raise HTTPException(503, "SSO not configured")

    # Lookup provider for workspace
    async with db.execute(
        "SELECT id, client_id FROM oidc_providers WHERE workspace_id = ? AND enabled = 1 LIMIT 1",
        (workspace,),
    ) as cursor:
        provider = await cursor.fetchone()
    if not provider:
        raise HTTPException(404, "No SSO provider configured for this workspace")

    # CSRF state token — stored in HttpOnly cookie
    state = secrets.token_urlsafe(32)
    # Include workspace in cookie value (trusted), not only in URL state
    state_cookie_value = json.dumps({"state": state, "workspace": workspace})
    state_payload = state_cookie_value  # same payload sent in URL state param

    organization_id = os.environ.get("MARVIS_WORKOS_ORG_ID", "").strip()
    if not organization_id:
        raise HTTPException(503, "SSO organization not configured")

    # Build WorkOS authorization URL
    try:
        import workos
        workos_client = workos.WorkOSClient(
            api_key=settings.workos_api_key,
            client_id=settings.workos_client_id,
        )
        # redirect_uri is HARDCODED — REVIEW FIX: never accept from request
        base_url = str(request.base_url).rstrip("/")
        redirect_uri = f"{base_url}/api/v1/auth/sso/callback"

        if _workos_authkit_enabled():
            auth_url = await asyncio.to_thread(
                workos_client.user_management.get_authorization_url,
                redirect_uri=redirect_uri,
                state=state_payload,
                organization_id=organization_id,
                provider="authkit",
            )
        else:
            auth_url = await asyncio.to_thread(
                workos_client.sso.get_authorization_url,
                redirect_uri=redirect_uri,
                state=state_payload,
                organization_id=organization_id,
            )
    except ImportError:
        raise HTTPException(503, "WorkOS SDK not installed")

    response = RedirectResponse(url=auth_url, status_code=302)
    response.set_cookie(
        key="sso_state",
        value=state_cookie_value,  # JSON with state + workspace
        httponly=True,
        secure=True,
        samesite="lax",
        max_age=600,  # 10 minutes
        domain=_workos_cookie_domain(),
    )
    return response


@router.get("/auth/sso/callback")
async def sso_callback(
    state: str,
    request: Request,
    code: str | None = None,
    error: str | None = None,
    error_description: str | None = None,
    db: aiosqlite.Connection = Depends(get_write_db),
):
    """Callback from WorkOS. Verifies state, exchanges code, creates/updates user, issues JWT."""
    if not settings.sso_enabled:
        raise HTTPException(404, "SSO not enabled")

    # CSRF validation: compare state URL param with trusted HttpOnly cookie value
    cookie_raw = request.cookies.get("sso_state", "")
    try:
        cookie_data = json.loads(cookie_raw)
    except (json.JSONDecodeError, TypeError):
        raise HTTPException(400, "Invalid SSO state")

    # Validate: the state URL param must match EXACTLY the cookie value (constant-time)
    if not secrets.compare_digest(cookie_raw, state):
        raise HTTPException(400, "Invalid SSO state — possible CSRF")

    # Extract workspace from TRUSTED COOKIE (not from attacker-controlled URL param)
    workspace_id = cookie_data.get("workspace")
    if not isinstance(workspace_id, str) or not workspace_id.strip():
        raise HTTPException(400, "Invalid SSO workspace state")
    workspace_id = workspace_id.strip()

    # The workspace comes from the state cookie, so prove it still has an
    # enabled SSO provider before spending the WorkOS code or provisioning users.
    async with db.execute(
        "SELECT allowed_email_domains FROM oidc_providers WHERE workspace_id = ? AND enabled = 1 LIMIT 1",
        (workspace_id,),
    ) as cursor:
        provider_row = await cursor.fetchone()

    if provider_row is None:
        raise HTTPException(403, "No SSO provider configured for this workspace")

    if error:
        detail = error_description or error
        logger.warning("SSO callback error from WorkOS: %s", detail)
        raise HTTPException(400, detail)
    if not code:
        raise HTTPException(422, "SSO code missing")

    # Exchange authorization code for user profile
    authkit_enabled = _workos_authkit_enabled()
    wos_session_id: str | None = None
    try:
        import workos
        workos_client = workos.WorkOSClient(
            api_key=settings.workos_api_key,
            client_id=settings.workos_client_id,
        )
        if authkit_enabled:
            auth_response = await asyncio.to_thread(
                workos_client.user_management.authenticate_with_code,
                code=code,
            )
            profile = auth_response.user
            # WorkOS AuthKit keeps its own session; capture its id so logout can
            # actually end it (clearing our cookie alone is not logout).
            wos_session_id = _sid_from_access_token(
                getattr(auth_response, "access_token", None)
            )
            configured_org_id = os.environ.get("MARVIS_WORKOS_ORG_ID", "").strip()
            auth_org_id = _require_authkit_org(auth_response, configured_org_id)
            memberships = await asyncio.to_thread(
                workos_client.user_management.list_organization_memberships,
                user_id=profile.id,
                organization_id=auth_org_id,
                statuses=["active"],
                limit=10,
            )
            workos_role = _require_active_membership_role(memberships)
        else:
            profile_and_token = await asyncio.to_thread(
                workos_client.sso.get_profile_and_token,
                code=code,
            )
            profile = profile_and_token.profile
            workos_role = getattr(profile, "role", "member") or "member"
    except ImportError:
        raise HTTPException(503, "WorkOS SDK not installed")
    except HTTPException:
        raise
    except Exception as e:
        logger.warning("SSO code exchange failed: %s", e)
        raise HTTPException(400, "SSO authentication failed")

    # Validate email domain if restrictions exist
    if provider_row["allowed_email_domains"]:
        try:
            allowed_domains = json.loads(provider_row["allowed_email_domains"])
        except (json.JSONDecodeError, TypeError):
            allowed_domains = []
        if allowed_domains:
            email_domain = profile.email.split("@")[1] if "@" in profile.email else ""
            if email_domain not in allowed_domains:
                raise HTTPException(403, "Email domain not allowed for this workspace")

    # Check email_verified
    if hasattr(profile, "email_verified") and not profile.email_verified:
        raise HTTPException(403, "Email not verified — cannot auto-provision")

    # Find or create user
    external_id = profile.id  # WorkOS profile ID
    email = profile.email
    display_name = getattr(profile, "first_name", "") or email.split("@")[0]
    mapped_role = _SSO_ROLE_MAPPING.get(workos_role, "viewer")

    # Auto-link only inside the workspace proven by the signed state cookie.
    # The same email may legitimately belong to another tenant and must never
    # be updated by this callback.
    async with db.execute(
        "SELECT id, slug, system_role FROM users "
        "WHERE lower(email) = lower(?) AND workspace_id = ? "
        "AND deleted_at IS NULL",
        (email, workspace_id),
    ) as cursor:
        existing_user = await cursor.fetchone()

    if existing_user:
        # AuthKit membership is authoritative for hosted roles. Legacy SSO
        # profiles do not carry the same verified organization membership
        # contract, so they retain the existing local role.
        user_role = existing_user["system_role"]
        if authkit_enabled and user_role != "super_admin":
            user_role = mapped_role
        await db.execute(
            "UPDATE users SET external_id = ?, auth_provider = 'workos', "
            "system_role = ? WHERE id = ? AND workspace_id = ?",
            (external_id, user_role, existing_user["id"], workspace_id),
        )
        await db.commit()
        user_slug = existing_user["slug"]
    else:
        # Auto-provision new user
        import uuid as uuid_mod
        user_id = uuid_mod.uuid4().hex[:32]
        legacy_slug = email.split("@")[0].lower().replace(".", "-")[:50]
        slug_taken = await (
            await db.execute("SELECT 1 FROM users WHERE slug = ?", (legacy_slug,))
        ).fetchone()
        user_slug = legacy_slug
        if not user_slug or slug_taken is not None:
            user_slug = "workos-" + hashlib.sha256(
                f"{workspace_id}\0{external_id}".encode("utf-8")
            ).hexdigest()[:16]
            fallback_taken = await (
                await db.execute("SELECT 1 FROM users WHERE slug = ?", (user_slug,))
            ).fetchone()
            if fallback_taken is not None:
                raise HTTPException(409, "SSO user identity conflict")
        # Map WorkOS role — capped at admin (REVIEW FIX: never super_admin via SSO)
        user_role = mapped_role
        now = datetime.now(timezone.utc).isoformat()

        await db.execute(
            "INSERT INTO users (id, slug, email, display_name, system_role, auth_provider, "
            "external_id, workspace_id, onboarding_completed, created_at) "
            "VALUES (?, ?, ?, ?, ?, 'workos', ?, ?, 0, ?)",
            (user_id, user_slug, email, display_name, user_role, external_id, workspace_id, now),
        )
        await db.commit()

    # Issue JWT (same as email/password flow) — includes workspace_id, plus the
    # WorkOS session id so logout can end the AuthKit session server-side.
    extra_claims = {"workspace_id": workspace_id}
    if wos_session_id:
        extra_claims["wos_sid"] = wos_session_id
    jwt_token, _jti, _expires = create_session_jwt(user_slug, extra_claims=extra_claims)

    # Clear state cookie, set session cookie, redirect to the hosted Console.
    response = RedirectResponse(url="/ui", status_code=302)
    response.set_cookie(
        key="pir_session",
        value=jwt_token,
        httponly=True,
        secure=True,
        samesite="lax",
        max_age=settings.jwt_expiry_hours * 3600,
        domain=_workos_cookie_domain(),
    )
    # Non-identifying hint on the parent domain so the marketing site can show a
    # signed-in header. The real session stays host-only above.
    set_signed_in_bit(response)
    response.delete_cookie("sso_state", domain=_workos_cookie_domain())
    return response


@router.get("/auth/sso/config")
async def sso_config(
    workspace: str = "ws_default",
    db: aiosqlite.Connection = Depends(get_db),
) -> dict:
    """Public endpoint: check if SSO is enabled for a workspace. Used by Console login page."""
    if not settings.sso_enabled:
        return {"enabled": False, "email_domains": [], "provider": None}

    async with db.execute(
        "SELECT provider_type FROM oidc_providers "
        "WHERE workspace_id = ? AND enabled = 1 LIMIT 1",
        (workspace,),
    ) as cursor:
        provider = await cursor.fetchone()

    return {
        "enabled": provider is not None,
        "email_domains": [],
        "provider": provider["provider_type"] if provider is not None else None,
    }


class TenantHandoffRejected(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class TenantHandoffPrincipal:
    sub: str
    org_id: str
    sid: str
    workos_role: str
    system_role: str
    assertion_jti: str
    assertion_expires_at: datetime
    workspace_id: str | None


class TenantHandoffVerifier:
    def __init__(
        self,
        *,
        public_key: bytes,
        tenant_id: str,
        workos_org_id: str,
        workspace_id: str | None = None,
    ) -> None:
        try:
            key = serialization.load_pem_public_key(public_key)
        except (TypeError, ValueError) as exc:
            raise ValueError("handoff verification key must be Ed25519 public PEM") from exc
        if not isinstance(key, Ed25519PublicKey):
            raise ValueError("handoff verification key must be Ed25519 public PEM")
        if not tenant_id or not workos_org_id:
            raise ValueError("tenant handoff identity must be configured")
        if workspace_id is not None and not workspace_id.strip():
            raise ValueError("tenant handoff workspace must be configured")
        self._key = key
        self._tenant_id = tenant_id
        self._org_id = workos_org_id
        self._workspace_id = workspace_id.strip() if workspace_id is not None else None

    def verify(
        self,
        login_assertion: str,
        wake_ticket: str,
    ) -> TenantHandoffPrincipal:
        assertion = self._decode(login_assertion, "login_assertion")
        wake = self._decode(wake_ticket, "wake_ticket")
        paired = ("sub", "org_id", "sid", "role", "handoff_id")
        if any(assertion.get(key) != wake.get(key) for key in paired):
            raise TenantHandoffRejected("invalid_handoff")
        role, known = map_sso_role(assertion.get("role"))
        if not known:
            raise TenantHandoffRejected("invalid_handoff")
        try:
            expires_at = datetime.fromtimestamp(
                int(assertion["exp"]), tz=timezone.utc
            )
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise TenantHandoffRejected("invalid_handoff") from exc
        return TenantHandoffPrincipal(
            sub=str(assertion["sub"]),
            org_id=str(assertion["org_id"]),
            sid=str(assertion["sid"]),
            workos_role=str(assertion["role"]),
            system_role=role,
            assertion_jti=str(assertion["jti"]),
            assertion_expires_at=expires_at,
            workspace_id=self._workspace_id,
        )

    def verify_wake(self, wake_ticket: str) -> TenantHandoffPrincipal:
        wake = self._decode(wake_ticket, "wake_ticket")
        role, known = map_sso_role(wake.get("role"))
        if not known:
            raise TenantHandoffRejected("invalid_handoff")
        try:
            expires_at = datetime.fromtimestamp(int(wake["exp"]), tz=timezone.utc)
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise TenantHandoffRejected("invalid_handoff") from exc
        return TenantHandoffPrincipal(
            sub=str(wake["sub"]),
            org_id=str(wake["org_id"]),
            sid=str(wake["sid"]),
            workos_role=str(wake["role"]),
            system_role=role,
            assertion_jti=str(wake["jti"]),
            assertion_expires_at=expires_at,
            workspace_id=self._workspace_id,
        )

    def _decode(self, token: str, expected_kind: str) -> dict[str, object]:
        if not isinstance(token, str) or not token or len(token) > 8192:
            raise TenantHandoffRejected("invalid_handoff")
        try:
            payload = jwt.decode(
                token,
                self._key,
                algorithms=["EdDSA"],
                audience=self._tenant_id,
                issuer="https://brain.justaskmarvis.com",
                leeway=5,
                options={
                    "require": [
                        "iss",
                        "aud",
                        "sub",
                        "org_id",
                        "sid",
                        "role",
                        "kind",
                        "handoff_id",
                        "jti",
                        "iat",
                        "nbf",
                        "exp",
                    ]
                },
            )
        except jwt.PyJWTError as exc:
            raise TenantHandoffRejected("invalid_handoff") from exc
        if payload.get("kind") != expected_kind or payload.get("org_id") != self._org_id:
            raise TenantHandoffRejected("invalid_handoff")
        for key in ("sub", "org_id", "sid", "role", "handoff_id", "jti"):
            if not isinstance(payload.get(key), str) or not payload[key]:
                raise TenantHandoffRejected("invalid_handoff")
        return payload


handoff_router = APIRouter()


def set_host_only_handoff_cookies(
    response: Response,
    *,
    session_token: str,
    wake_ticket: str,
) -> None:
    """Set tenant-host cookies; omitting Domain is a security boundary."""
    for key, value, max_age in (
        ("pir_session", session_token, settings.jwt_expiry_hours * 3600),
        ("__Host-marvis_wake", wake_ticket, 15),
    ):
        response.set_cookie(
            key=key,
            value=value,
            httponly=True,
            secure=True,
            samesite="lax",
            max_age=max_age,
            path="/",
        )


def _one_header(request: Request, name: str) -> str | None:
    values = request.headers.getlist(name)
    if len(values) != 1:
        return None
    return values[0]


def _valid_handoff_return_path(path: str | None) -> bool:
    if not isinstance(path, str):
        return False
    allowed = (
        path == "/ui"
        or path.startswith("/ui/")
        or path.startswith("/api/v1/")
        or path.startswith("/auth/")
    )
    return (
        allowed
        and not path.startswith("//")
        and "\\" not in path
        and not any(ord(character) < 32 for character in path)
    )


@handoff_router.get("/auth/handoff")
async def tenant_auth_handoff(
    request: Request,
    db: aiosqlite.Connection = Depends(get_write_db),
):
    if request.query_params:
        return JSONResponse({"code": "invalid_handoff_request"}, status_code=400)
    verifier = getattr(request.app.state, "tenant_handoff_verifier", None)
    if not isinstance(verifier, TenantHandoffVerifier):
        return JSONResponse({"code": "handoff_unavailable"}, status_code=503)
    assertion = _one_header(request, "x-marvis-login-assertion")
    wake_ticket = _one_header(request, "x-marvis-wake-ticket")
    return_path = _one_header(request, "x-marvis-return-path")
    if assertion is None or wake_ticket is None or not _valid_handoff_return_path(return_path):
        return JSONResponse({"code": "invalid_handoff"}, status_code=403)
    try:
        principal = verifier.verify(assertion, wake_ticket)
    except TenantHandoffRejected:
        return JSONResponse({"code": "invalid_handoff"}, status_code=403)
    if principal.workspace_id is None:
        return JSONResponse({"code": "handoff_unavailable"}, status_code=503)
    try:
        await db.execute(
            "INSERT INTO token_blacklist(jti, expires_at) VALUES(?, ?)",
            (principal.assertion_jti, principal.assertion_expires_at.isoformat()),
        )
    except sqlite3.IntegrityError:
        await db.rollback()
        return JSONResponse({"code": "invalid_handoff"}, status_code=403)
    try:
        cursor = await db.execute(
            """
            SELECT id, slug, system_role, workspace_id
            FROM users
            WHERE auth_provider='workos' AND external_id=? AND workspace_id=?
              AND deleted_at IS NULL
            LIMIT 2
            """,
            (principal.sub, principal.workspace_id),
        )
        users = await cursor.fetchall()
        if len(users) > 1:
            raise TenantHandoffRejected("invalid_handoff")
        if users:
            user_slug = str(users[0]["slug"])
            if users[0]["system_role"] != "super_admin":
                await db.execute(
                    "UPDATE users SET system_role=? WHERE id=?",
                    (principal.system_role, users[0]["id"]),
                )
        else:
            user_slug = "workos-" + hashlib.sha256(
                principal.sub.encode("utf-8")
            ).hexdigest()[:16]
            collision = await db.execute(
                "SELECT 1 FROM users WHERE slug=?", (user_slug,)
            )
            if await collision.fetchone() is not None:
                # Preserve legacy slugs when free, but never reuse a sibling
                # workspace identity. The fallback remains deterministic for
                # repeated handoffs to this exact workspace.
                user_slug = "workos-" + hashlib.sha256(
                    f"{principal.workspace_id}\0{principal.sub}".encode("utf-8")
                ).hexdigest()[:16]
                workspace_collision = await db.execute(
                    "SELECT 1 FROM users WHERE slug=?", (user_slug,)
                )
                if await workspace_collision.fetchone() is not None:
                    raise TenantHandoffRejected("invalid_handoff")
            await db.execute(
                """
                INSERT INTO users(
                    id, slug, display_name, type, email, system_role,
                    auth_provider, external_id, workspace_id
                ) VALUES (?, ?, 'Marvis user', 'human', NULL, ?, 'workos', ?, ?)
                """,
                (
                    uuid4().hex,
                    user_slug,
                    principal.system_role,
                    principal.sub,
                    principal.workspace_id,
                ),
            )
        await db.commit()
    except TenantHandoffRejected:
        await db.rollback()
        return JSONResponse({"code": "invalid_handoff"}, status_code=403)
    except Exception:
        await db.rollback()
        return JSONResponse({"code": "handoff_unavailable"}, status_code=503)
    session_token, _jti, _expires = create_session_jwt(
        user_slug,
        extra_claims={"workspace_id": principal.workspace_id},
    )
    response = RedirectResponse(url=return_path, status_code=302)
    set_host_only_handoff_cookies(
        response,
        session_token=session_token,
        wake_ticket=wake_ticket,
    )
    return response
