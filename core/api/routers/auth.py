# v2.3.0 - 2026-04-14 - Single-writer: login uses get_write_db (batch 5/6)
from __future__ import annotations

import asyncio
import json
import logging
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

from core.api.config import settings
from core.api.db import acquire_write_db, get_db, get_write_db
from core.api.models import (
    HealthResponse,
    TicketRequest,
    TicketResponse,
    UserInfo,
)
from core.api.rbac import require_role
from core.api.security import (
    blacklist_token,
    clear_auth_cookie,
    create_session_jwt,
    create_ws_ticket,
    get_current_user,
    set_auth_cookie,
    verify_session_jwt,
)
from core.api.services.terminal_metrics import TerminalMetricsCollector

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


async def _clear_password_must_change(db: aiosqlite.Connection, user_id: str) -> None:
    if not settings.force_password_change_on_first_login:
        return
    if not await _users_column_exists(db, "password_must_change"):
        return
    await db.execute(
        "UPDATE users SET password_must_change = 0, updated_at = datetime('now') WHERE id = ?",
        [user_id],
    )


def _terminal_metrics_collector(request: Request) -> TerminalMetricsCollector | None:
    collector = getattr(request.app.state, "terminal_metrics", None)
    return collector if isinstance(collector, TerminalMetricsCollector) else None


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
    # CF-Connecting-IP preferred for rate limiting (Cloudflare proxy)
    client_ip = (
        request.headers.get("CF-Connecting-IP")
        or (request.client.host if request.client else "unknown")
    )
    _check_rate_limit(client_ip)

    # Lookup user by email (case-insensitive), human type only
    async with db.execute(
        "SELECT id, slug, password_hash FROM users "
        "WHERE lower(email) = lower(?) AND type = 'human' AND deleted_at IS NULL",
        [body.email],
    ) as cursor:
        row = await cursor.fetchone()

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

    if await _password_must_change(db, row["id"]):
        serializer = _get_reset_serializer()
        token = serializer.dumps({"user_id": row["id"], "slug": row["slug"]})
        return JSONResponse(
            status_code=403,
            content={
                "status": "password_change_required",
                "reset_token": token,
                "user_id": row["id"],
            },
        )

    slug = row["slug"]
    token, _jti, _expires_at = create_session_jwt(slug)
    response = JSONResponse(content={"status": "ok"})
    set_auth_cookie(response, token)
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

    return {
        "username": current_user.username,
        "user_id": current_user.user_id,
        "system_role": current_user.system_role,
        "display_name": current_user.display_name,
        "teams": teams,
    }


@router.post("/auth/logout")
async def logout(
    request: Request,
    db: aiosqlite.Connection = Depends(get_write_db),
):
    """Blacklist current token and clear cookie."""
    token = request.cookies.get("pir_session")
    if token:
        try:
            payload = verify_session_jwt(token)
            jti = payload.get("jti")
            if jti:
                expires_at = datetime.fromtimestamp(payload["exp"], tz=timezone.utc)
                await blacklist_token(jti, expires_at, db)
        except Exception:
            pass  # Token already invalid, just clear cookie

    response = JSONResponse(content={"status": "ok"})
    clear_auth_cookie(response)
    return response


@router.post("/auth/admin/issue-reset-token")
async def issue_reset_token(
    body: IssueResetTokenRequest,
    caller: UserInfo = Depends(require_role("admin", "super_admin", human_only=True)),
    db: aiosqlite.Connection = Depends(get_write_db),
):
    """Issue a password-reset token for a human user. Admin+ only."""
    async with db.execute(
        "SELECT id, slug, password_hash, type FROM users WHERE id = ? AND deleted_at IS NULL",
        [body.user_id],
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
    token = serializer.dumps({"user_id": row["id"], "slug": row["slug"]})

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
    if not user_id:
        raise HTTPException(status_code=400, detail="Invalid reset token payload")

    # Step 2: verify user still exists
    async with db.execute(
        "SELECT id, slug FROM users WHERE id = ? AND deleted_at IS NULL",
        [user_id],
    ) as cursor:
        row = await cursor.fetchone()

    if row is None:
        raise HTTPException(status_code=404, detail="User not found")

    # Step 3: hash new password and update
    new_hash = bcrypt.hashpw(body.new_password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
    now = datetime.now(timezone.utc).isoformat()
    await db.execute(
        "UPDATE users SET password_hash = ?, updated_at = ? WHERE id = ?",
        [new_hash, now, user_id],
    )
    await _clear_password_must_change(db, user_id)
    await db.commit()

    logger.info("Password reset completed for user %s", row["slug"])
    return {"status": "ok", "slug": row["slug"]}


@router.post("/terminal/ticket", response_model=TicketResponse)
async def get_terminal_ticket(
    body: TicketRequest,
    request: Request,
    user: UserInfo = Depends(require_role("operator", "admin", "super_admin", human_only=True)),
):
    """Generate opaque WS ticket for terminal connection. Operator+ human-only."""
    collector = _terminal_metrics_collector(request)
    started = time.perf_counter()
    lock_wait_ms: float | None = None
    timings: dict[str, float | str] = {}
    try:
        # In-memory ticket store — no write_lock, no DB. lock_wait stays ~0.
        lock_started = time.perf_counter()
        ticket = await create_ws_ticket(
            user.username,
            body.session_name,
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

# Default role mapping: WorkOS role → MarvisX system_role
# Cap at admin — super_admin only via manual seed/promotion
_SSO_ROLE_MAPPING: dict[str, str] = {
    "owner": "admin",  # capped — never auto-assign super_admin
    "admin": "admin",
    "member": "operator",
    "guest": "viewer",
}


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

        auth_url = await asyncio.to_thread(
            workos_client.sso.get_authorization_url,
            redirect_uri=redirect_uri,
            state=state_payload,
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
        domain=settings.cookie_domain,
    )
    return response


@router.get("/auth/sso/callback")
async def sso_callback(
    code: str,
    state: str,
    request: Request,
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
    workspace_id = cookie_data.get("workspace", "ws_default")

    # Exchange authorization code for user profile
    try:
        import workos
        workos_client = workos.WorkOSClient(
            api_key=settings.workos_api_key,
            client_id=settings.workos_client_id,
        )
        profile_and_token = await asyncio.to_thread(
            workos_client.sso.get_profile_and_token,
            code=code,
        )
        profile = profile_and_token.profile
    except ImportError:
        raise HTTPException(503, "WorkOS SDK not installed")
    except Exception as e:
        logger.warning("SSO code exchange failed: %s", e)
        raise HTTPException(400, "SSO authentication failed")

    # Validate email domain if restrictions exist
    async with db.execute(
        "SELECT allowed_email_domains FROM oidc_providers WHERE workspace_id = ? AND enabled = 1 LIMIT 1",
        (workspace_id,),
    ) as cursor:
        provider_row = await cursor.fetchone()

    if provider_row and provider_row["allowed_email_domains"]:
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

    # Auto-link by email if user exists locally
    async with db.execute(
        "SELECT id, slug, system_role FROM users WHERE email = ? AND deleted_at IS NULL",
        (email,),
    ) as cursor:
        existing_user = await cursor.fetchone()

    if existing_user:
        # Link existing local user to SSO provider
        await db.execute(
            "UPDATE users SET external_id = ?, auth_provider = 'workos' WHERE id = ?",
            (external_id, existing_user["id"]),
        )
        await db.commit()
        user_slug = existing_user["slug"]
        user_role = existing_user["system_role"]
    else:
        # Auto-provision new user
        import uuid as uuid_mod
        user_id = uuid_mod.uuid4().hex[:32]
        user_slug = email.split("@")[0].lower().replace(".", "-")[:50]
        # Map WorkOS role — capped at admin (REVIEW FIX: never super_admin via SSO)
        workos_role = getattr(profile, "role", "member") or "member"
        user_role = _SSO_ROLE_MAPPING.get(workos_role, "viewer")
        now = datetime.now(timezone.utc).isoformat()

        await db.execute(
            "INSERT INTO users (id, slug, email, display_name, system_role, auth_provider, "
            "external_id, workspace_id, onboarding_completed, created_at) "
            "VALUES (?, ?, ?, ?, ?, 'workos', ?, ?, 0, ?)",
            (user_id, user_slug, email, display_name, user_role, external_id, workspace_id, now),
        )
        await db.commit()

    # Issue JWT (same as email/password flow) — includes workspace_id
    jwt_token, _jti, _expires = create_session_jwt(user_slug, extra_claims={"workspace_id": workspace_id})

    # Clear state cookie, set session cookie, redirect to Console
    response = RedirectResponse(url="/", status_code=302)
    response.set_cookie(
        key="pir_session",
        value=jwt_token,
        httponly=True,
        secure=True,
        samesite="lax",
        max_age=settings.jwt_expiry_hours * 3600,
        domain=settings.cookie_domain,
    )
    response.delete_cookie("sso_state", domain=settings.cookie_domain)
    return response


@router.get("/auth/sso/config")
async def sso_config(
    workspace: str = "ws_default",
    db: aiosqlite.Connection = Depends(get_db),
) -> dict:
    """Public endpoint: check if SSO is enabled for a workspace. Used by Console login page."""
    if not settings.sso_enabled:
        return {"sso_enabled": False}

    async with db.execute(
        "SELECT id FROM oidc_providers WHERE workspace_id = ? AND enabled = 1 LIMIT 1",
        (workspace,),
    ) as cursor:
        has_provider = await cursor.fetchone() is not None

    return {"sso_enabled": has_provider}
