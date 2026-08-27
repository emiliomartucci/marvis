# v1.1.0 - 2026-05-27 - S1 F3.0: add LOCAL_CTX singleton + db/result wiring helpers (MCP server skeleton)
"""MCP adapter: domain<->MCP wiring for the Python MCP server.

Two responsibilities, both transport-thin:

1. ``raise_mcp_error(ServiceError)`` — maps a domain :class:`ServiceError` to the
   SDK-native tool-error channel by RAISING ``ToolError(f"{code}: {message}")``.
   FastMCP catches the raised ``ToolError`` and emits a real
   ``CallToolResult(isError=True, content=[text=...])`` — the only mechanism a
   client (Claude Code) recognises as a tool error. The MCP surface ignores
   ``err.http_status`` (HTTP is not its transport) and maps ``code`` + ``message``
   to the error text. This is uniform across EVERY tool regardless of return type:
   raising bypasses FastMCP's structured-output validation, so it also works for
   the ``-> list`` tools (which could not return the old error dict — it failed the
   ``list`` output schema). See ``to_mcp_error`` below for why the returned-dict
   shape was abandoned (verified empirically: a returned dict is success DATA, not
   a protocol ``isError``).
2. Identity wiring: :data:`LOCAL_CTX` is restricted to trusted local stdio while
   remote calls resolve a verified token; plus :func:`dump` and DB acquire helpers
   re-exported so ``tools/*.py`` import db access from one place.

The ``ToolError`` import is local to :func:`raise_mcp_error` so the rest of the
module (``LOCAL_CTX`` + the seam callables) stays testable without the ``mcp`` SDK
installed (parity with ``server.py``'s function-local SDK import).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from collections.abc import Mapping
from typing import Any, Literal, NoReturn

from core.api.use_cases._context import CallerContext, require_workspace_ctx
from core.api.use_cases._errors import NotFoundError, ServiceError
from core.api.use_cases._roles import map_sso_role

logger = logging.getLogger(__name__)
_TOOL_ERROR_RUNTIME: Literal["mcp", "fastmcp"] = "mcp"

# Re-export the db context managers so tools import db access from the adapter,
# not from deep in the api package. ``acquire_db`` = read pool; ``acquire_write_db``
# = writer + lock (mutators). Both are @asynccontextmanager importable directly.
from core.api.db import acquire_db, acquire_write_db  # noqa: F401  (re-export)

# ---------------------------------------------------------------------------
# MCP identity.
# ---------------------------------------------------------------------------
# The MCP process IS the lifetime container, so this is a module singleton (the
# `app.state` equivalent for a process with no FastAPI `app`). Every tool calls
# the SAME use_cases the HTTP adapter calls; only CallerContext fill differs.
# MCP is agentic by default, so approval remains in Console/Triage.
def _env_flag(
    name: str,
    *,
    default: bool = False,
    env: Mapping[str, str] | None = None,
) -> bool:
    source = os.environ if env is None else env
    raw = source.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _build_local_ctx_from_env(env: Mapping[str, str] | None = None) -> CallerContext:
    """Build the trusted local single-user identity.

    ``env`` remains an accepted argument for backwards-compatible tests/callers,
    but environment values never select a principal, role, workspace, reviewer,
    or human authority. Remote transports resolve only verified access tokens.
    """
    del env
    return CallerContext.local_single_user()


# Trusted local stdio is the OSS single-user/CLI surface. Remote HTTP calls never
# fall back to this identity; they require the verified FastMCP access token.
LOCAL_CTX: CallerContext = _build_local_ctx_from_env()
_REMOTE_UNAUTHENTICATED_CTX = CallerContext(
    username="unauthenticated",
    system_role="viewer",
    user_type="agent",
    workspace_id="",
    user_id="",
)
_MCP_TRANSPORT_MODE: Literal["stdio", "http"] | None = None


def set_mcp_transport_mode(transport: Literal["stdio", "http"] | None) -> None:
    """Select whether missing-token calls may use the trusted local principal."""
    global _MCP_TRANSPORT_MODE
    _MCP_TRANSPORT_MODE = transport


def _no_token_mcp_context() -> CallerContext:
    if _MCP_TRANSPORT_MODE == "http":
        return _REMOTE_UNAUTHENTICATED_CTX
    if _MCP_TRANSPORT_MODE == "stdio":
        return LOCAL_CTX
    raw_transport = os.environ.get("MARVIS_MCP_TRANSPORT", "stdio").strip().lower()
    if raw_transport in {
        "http",
        "streamable-http",
        "streamable_http",
    }:
        return _REMOTE_UNAUTHENTICATED_CTX
    return LOCAL_CTX


# Interactive AuthKit OAuth2 tokens carry only org_id/sub/sid — never a `role`
# claim (empirically verified 2026-07-02: the authorization-code token omits
# role/roles/permissions; only User Management password-grant tokens include
# them). Without a fallback EVERY real browser/connector login would default to
# viewer fleet-wide. When the claim is absent, honor the persisted
# users.system_role (seeded by provisioning/add_user, by an admin, or by a prior
# claim-bearing sync). This readonly, TTL-cached lookup keeps the sync identity
# path off the async DB pool and out of the hot path; it can only RESTORE a role
# the DB already recorded — never an escalation.
_DB_ROLE_CACHE: dict[tuple[str, str], tuple[str | None, float]] = {}
_DB_ROLE_TTL_SECONDS = 30.0


def _db_system_role(user_id: str, workspace_id: str) -> str | None:
    """Persisted ``users.system_role`` for an OAuth person, or None when unknown.

    Readonly + TTL-cached so the sync identity path never blocks on or writes to
    the DB. Fail-closed: any error or missing row yields None and the caller
    keeps the viewer default.
    """
    if not user_id or user_id == "local" or not workspace_id:
        return None
    now = time.monotonic()
    cache_key = (workspace_id, user_id)
    cached = _DB_ROLE_CACHE.get(cache_key)
    if cached is not None and cached[1] > now:
        return cached[0]
    role: str | None = None
    db_path = os.environ.get("MARVIS_DB_PATH")
    if db_path:
        import sqlite3

        try:
            con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2.0)
            try:
                cur = con.execute(
                    "SELECT system_role FROM users"
                    " WHERE id = ? AND workspace_id = ?"
                    " AND deleted_at IS NULL LIMIT 1",
                    (user_id, workspace_id),
                )
                row = cur.fetchone()
            finally:
                con.close()
            role = str(row[0]) if row and row[0] else None
            if role and role != "viewer":
                logger.info(
                    "oauth role: DB fallback %s -> %s (interactive token lacks role claim)",
                    user_id,
                    role,
                )
        except Exception:
            logger.warning(
                "current_mcp_context: DB role lookup failed for %s",
                user_id,
                exc_info=True,
            )
            role = None
    _DB_ROLE_CACHE[cache_key] = (role, now + _DB_ROLE_TTL_SECONDS)
    return role


def _invalidate_db_role_cache(user_id: str, workspace_id: str) -> None:
    """Drop a cached role so the next request re-reads a freshly-synced row."""
    _DB_ROLE_CACHE.pop((workspace_id, user_id), None)


def _authenticated_mcp_workspace(claims: Mapping[str, Any]) -> str:
    """Resolve an exact remote workspace or reject ambiguous authentication."""
    configured = os.environ.get("MARVIS_MCP_WORKSPACE_ID", "").strip()
    raw_claim = claims.get("workspace_id")
    claimed = str(raw_claim).strip() if raw_claim is not None else ""
    if configured and claimed and configured != claimed:
        raise RuntimeError(
            "Authenticated MCP workspace claim does not match server configuration"
        )
    workspace_id = claimed or configured
    if not workspace_id:
        raise RuntimeError(
            "MARVIS_MCP_WORKSPACE_ID is required for authenticated remote MCP calls"
        )
    return workspace_id


def current_mcp_context() -> CallerContext:
    """Resolve the current MCP caller when the transport exposes auth context.

    Static tenant Bearer tokens remain tenant-admin/full-access by design. OAuth
    tokens carry a person subject and are intentionally least-privilege here; the
    access_grants predicate resolves their project access.
    """
    try:
        from fastmcp.server.dependencies import get_access_token

        access_token = get_access_token()
    except Exception:
        return _no_token_mcp_context()
    if access_token is None:
        return _no_token_mcp_context()

    claims = getattr(access_token, "claims", None) or {}
    client_id = getattr(access_token, "client_id", None) or claims.get("client_id")
    scopes = tuple(getattr(access_token, "scopes", None) or claims.get("scopes") or ())
    tenant_id = os.environ.get("TENANT_ID", "").strip()
    workspace_id = _authenticated_mcp_workspace(claims)

    # StaticTokenVerifier in server.py sets client_id to TENANT_ID and has no
    # person claim. That token is the tenant admin break-glass/admin path.
    if client_id and tenant_id and client_id == tenant_id and not claims.get("sub"):
        return CallerContext(
            username=f"{tenant_id}:static-bearer",
            system_role="admin",
            user_type="agent",
            workspace_id=workspace_id,
            scopes=scopes,
            user_id=f"tenant:{tenant_id}",
        )

    subject = (
        claims.get("sub")
        or claims.get("email")
        or claims.get("username")
        or client_id
        or "unauthenticated"
    )
    # Entra tokens carry the stable object id in `oid` — grants must anchor to
    # it, not the rotating pairwise sub (IMPL §A.0c, security A2). No-op for
    # WorkOS tokens (no oid claim).
    user_id = str(claims.get("user_id") or claims.get("oid") or subject or "")
    raw_role = _role_claim(claims)
    system_role, role_known = map_sso_role(raw_role)
    if raw_role is not None and not role_known:
        logger.warning(
            "unknown SSO role claim %r for OAuth subject %s; defaulting to viewer",
            raw_role,
            subject,
        )
    elif raw_role is None:
        # Interactive AuthKit token: no role claim. Fall back to the persisted
        # role so a real browser login is not silently downgraded to viewer.
        db_role = _db_system_role(user_id, workspace_id)
        if db_role:
            system_role = db_role
    # Entra client-credentials (M2M) tokens declare idtyp=app — that caller is
    # an agent, not a person (IMPL §A.0c). No-op for WorkOS tokens.
    is_app_token = claims.get("idtyp") == "app"
    return CallerContext(
        username=str(subject),
        system_role=system_role,
        user_type="agent" if is_app_token else "human",
        workspace_id=workspace_id,
        scopes=scopes,
        user_id=user_id,
        is_human_session=not is_app_token,
    )


def _role_claim(claims: Mapping[str, Any]) -> object:
    raw = claims.get("role")
    if raw is None:
        roles = claims.get("roles")
        if isinstance(roles, (list, tuple)) and roles:
            return roles[0]
    return raw


def _oauth_claims() -> Mapping[str, Any] | None:
    try:
        from fastmcp.server.dependencies import get_access_token

        access_token = get_access_token()
    except Exception:
        return None
    if access_token is None:
        return None
    claims = getattr(access_token, "claims", None) or {}
    # The static tenant bearer has no person subject — nothing to sync.
    if not claims.get("sub"):
        return None
    return claims


def oauth_user_sync_decision(
    existing_role: str | None, raw_role: object
) -> tuple[str, str] | None:
    """Decide the users-row write for an OAuth person: (action, role) or None.

    No write when the claim is absent/unknown (a defaulted "viewer" must never
    overwrite a real role), when the row already matches, or when the existing
    row is super_admin (never down-synced).
    """
    mapped, known = map_sso_role(raw_role)
    if not known:
        return None
    if existing_role is None:
        return ("insert", mapped)
    if existing_role == "super_admin" or existing_role == mapped:
        return None
    return ("update", mapped)


# One write per (user, role) per process lifetime; every other call is a
# single read on the already-open handle.
_SYNCED_OAUTH_USERS: dict[tuple[str, str], str] = {}


async def sync_oauth_user(db, ctx: CallerContext) -> None:
    """Display-only drift sync of the ``users`` row for an OAuth person."""
    if not ctx.is_human_session or not ctx.user_id or ctx.user_id == "local":
        return
    claims = _oauth_claims()
    if claims is None:
        return
    workspace_id = require_workspace_ctx(ctx)
    raw_role = _role_claim(claims)
    mapped, known = map_sso_role(raw_role)
    if not known:
        if raw_role is not None:
            logger.warning(
                "sync_oauth_user: unmapped role claim %r for %s — no write",
                raw_role,
                ctx.user_id,
            )
        return
    cache_key = (workspace_id, ctx.user_id)
    if _SYNCED_OAUTH_USERS.get(cache_key) == mapped:
        return
    try:
        cur = await db.execute(
            "SELECT system_role FROM users WHERE id = ? AND workspace_id = ?"
            " AND deleted_at IS NULL LIMIT 1",
            (ctx.user_id, workspace_id),
        )
        row = await cur.fetchone()
    except Exception:
        logger.warning(
            "sync_oauth_user: users lookup failed for %s", ctx.user_id, exc_info=True
        )
        return
    existing = str(row[0]) if row is not None else None
    decision = oauth_user_sync_decision(existing, raw_role)
    if decision is None:
        _SYNCED_OAUTH_USERS[cache_key] = mapped
        return
    action, role = decision
    email = claims.get("email") if isinstance(claims.get("email"), str) else None
    slug_source = (email.split("@", 1)[0] if email else ctx.user_id).lower()
    slug = re.sub(r"[^a-z0-9_-]+", "-", slug_source).strip("-") or ctx.user_id.lower()
    display_name = str(claims.get("name") or email or ctx.username or ctx.user_id)
    try:
        async with acquire_write_db(label="mcp.sync_oauth_user") as wdb:
            if action == "insert":
                await wdb.execute(
                    "INSERT OR IGNORE INTO users"
                    " (id, slug, display_name, email, system_role, type,"
                    " auth_provider, workspace_id)"
                    " VALUES (?, ?, ?, ?, ?, 'human', 'workos', ?)",
                    (ctx.user_id, slug, display_name, email, role, workspace_id),
                )
            else:
                await wdb.execute(
                    "UPDATE users SET system_role = ?,"
                    " updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')"
                    " WHERE id = ? AND workspace_id = ?"
                    " AND system_role != 'super_admin'",
                    (role, ctx.user_id, workspace_id),
                )
            await wdb.commit()
        _SYNCED_OAUTH_USERS[cache_key] = mapped
        _invalidate_db_role_cache(ctx.user_id, workspace_id)
        logger.info(
            "sync_oauth_user: %s system_role -> %s (was %s)",
            ctx.user_id,
            role,
            existing,
        )
    except Exception:
        logger.warning(
            "sync_oauth_user: upsert failed for %s — role stays per-request",
            ctx.user_id,
            exc_info=True,
        )


NO_GRANTS_CODE = "no_project_grants"
NO_GRANTS_MESSAGE = (
    "Nessun progetto ti è stato concesso su questo tenant — chiedi a un admin "
    "del tenant un grant di progetto (grant_access) o l'aggiunta a un team."
)


def no_grants_notice(visible_projects: set[str] | None) -> dict[str, str] | None:
    """Keys to merge into a dict-shaped tool response at zero visibility."""
    if visible_projects is not None and not visible_projects:
        return {"notice": NO_GRANTS_CODE, "message": NO_GRANTS_MESSAGE}
    return None


def require_any_grant(visible_projects: set[str] | None) -> None:
    """List-shaped tools have a fixed output schema: zero visibility raises an
    actionable error instead of returning a mute empty list."""
    if visible_projects is not None and not visible_projects:
        raise ServiceError(code=NO_GRANTS_CODE, message=NO_GRANTS_MESSAGE)


def person_user_id(ctx: CallerContext | None = None) -> str | None:
    """The DB ``users.id`` of the current caller IFF it is a real person, else None.

    Notifications and their notices are per-PERSON (``users.id``). The static tenant
    Bearer (``user_id='tenant:<id>'``, ``user_type='agent'``), non-person agents, and
    the stdio ``local``/seeded identity have no personal notification inbox — this
    guard returns None for all of them so the notices/list/ack surface is simply
    empty (never an error, never someone else's rows). Shared by the notifications
    use_case (effective user) and ``attach_notices`` (F4: bearer -> field absent).
    """
    resolved = ctx or current_mcp_context()
    if getattr(resolved, "user_type", None) != "human":
        return None
    uid = (getattr(resolved, "user_id", "") or "").strip()
    if not uid or uid == "local" or uid.startswith("tenant:"):
        return None
    return uid


async def current_visible_projects(db, ctx: CallerContext | None = None):
    from core.api.services import access_grants

    resolved = ctx or current_mcp_context()
    # Chokepoint shared by every visibility-aware tool: keep the person's
    # users row in sync (drift-only; never blocks the read path).
    await sync_oauth_user(db, resolved)
    return await access_grants.visible_projects_for_actor(db, resolved)


async def require_unambiguous_visible_project(
    db,
    ctx: CallerContext,
    project_slug: str,
    visible_projects: set[str] | None,
) -> None:
    """Deny remote slug-backed files unless one workspace owns the visible slug.

    Disk-backed project artifacts have no ``workspace_id`` column. Trusted local
    stdio keeps its single-user filesystem contract; every remote caller must
    carry an exact workspace, have the slug in its resolved grants, and be the
    sole owner recorded in ``workspace_projects``. Missing/old schema and shared
    slugs fail closed without revealing the competing workspace.
    """
    if ctx is LOCAL_CTX:
        return
    project = (project_slug or "").strip()
    if visible_projects is None or project not in visible_projects:
        raise NotFoundError(code="project_not_found", message="Project not found")
    from core.api.services.access_grants import require_unique_project_for_actor

    await require_unique_project_for_actor(db, ctx, project)


_UNSET_VISIBLE = object()


async def attach_notices(
    db,
    response,
    ctx: CallerContext | None = None,
    *,
    visible_projects=_UNSET_VISIBLE,
    project: str | None = None,
    task: str | None = None,
):
    """Merge a ``notices`` summary into a dict-shaped entry-tool response (F4).

    ASYNC on the tool's OWN connection — the entry tools already hold an
    ``acquire_db()`` handle, so this never opens a (blocking) sync connection.
    Absent for a bearer/agent caller (no personal inbox, via ``person_user_id``)
    and absent when nothing actionable is unread, so a caller pays zero token tax
    when there is nothing to close. Read-time visibility filtered (a revoked-grant
    project and company-scope brain never even enter the counter). Best-effort: any
    error omits ``notices`` rather than breaking the entry tool.
    """
    if not isinstance(response, dict):
        return response
    resolved = ctx or current_mcp_context()
    uid = person_user_id(resolved)
    if not uid:
        return response
    try:
        vis = (
            await current_visible_projects(db, resolved)
            if visible_projects is _UNSET_VISIBLE
            else visible_projects
        )
        from core.api.use_cases.notifications import count_unread_notices

        counts = await count_unread_notices(
            resolved,
            db,
            effective_user_id=uid,
            visible_projects=vis,
            project=project,
            task=task,
        )
    except Exception:  # noqa: BLE001 — notices is additive; never break the tool
        logger.debug("attach_notices: count failed; omitting notices", exc_info=True)
        return response

    # F2 onboarding nudge: computed from user_onboarding state (not a
    # notification row). Aggregated (one kind), surfaced on the cold-start
    # entries (task is None: session_brief / get_project), never on get_task.
    onb = None
    if task is None:
        try:
            from core.api.use_cases.onboarding_wizard import onboarding_pending

            onb = await onboarding_pending(
                db, workspace_id=resolved.workspace_id, user_id=uid
            )
        except Exception:  # noqa: BLE001 - additive; never break the tool
            logger.debug("attach_notices: onboarding count failed; omitting", exc_info=True)
            onb = None

    kinds = dict(counts)
    notif_total = sum(counts.values())
    show_onb = bool(onb) and onb.get("actionable", 0) > 0
    if show_onb:
        kinds["onboarding"] = 1
    total = notif_total + (1 if show_onb else 0)
    if total <= 0:
        return response

    hint_parts: list[str] = []
    if notif_total > 0:
        hint_parts.append(
            f"hai {notif_total} cosa/e aperta/e → list_notifications per vederle, "
            "ack_notification per archiviarle"
        )
    if show_onb:
        hint_parts.append(
            f"il tutorial non è finito ({onb['remaining']}/{onb['total']}) → "
            "onboarding_status per riprenderlo, "
            "onboarding_answer(step_key='all', action='skip') per chiuderlo"
        )
    response["notices"] = {
        "unread": total,
        "kinds": kinds,
        "hint": "da Marvis: " + "; ".join(hint_parts) + "; oppure ignora.",
    }
    return response


def current_user_info(ctx: CallerContext | None = None):
    """Adapt the resolved MCP caller to a ``UserInfo`` for the brain use_cases.

    The brain read/write use_cases resolve visibility through a ``UserInfo``
    (``get_visible_projects`` is duck-typed on system_role/user_type/user_id/
    workspace_id/scopes), whereas the other tool groups pass a pre-resolved
    ``visible_projects`` set. This is the "ctx -> UserInfo" adapter the brain
    RBAC fix (2026-07-03) needs.

    Admin / super_admin — and the static tenant bearer, which resolves to
    ``system_role='admin'`` — return ``None``: the brain services treat
    ``user is None`` as "no visibility restriction", so the admin/bearer path
    stays byte-identical to today (no new query, unrestricted). Every other
    caller (operator/viewer, OAuth person or non-admin agent) gets a real
    ``UserInfo`` so the services filter by their visible projects.
    """
    from core.api.models import UserInfo

    resolved = ctx or current_mcp_context()
    if resolved.system_role in ("admin", "super_admin"):
        return None
    return UserInfo(
        username=resolved.username,
        user_id=resolved.user_id,
        system_role=resolved.system_role,
        user_type=resolved.user_type,
        workspace_id=resolved.workspace_id,
        scopes=list(getattr(resolved, "scopes", ()) or ()),
    )


# ---------------------------------------------------------------------------
# MCP-local seam callables (the fastapi-free counterparts of the router seams).
# ---------------------------------------------------------------------------
# create_task / update_task use_cases take the side-effect hooks
# (sync_graph / schedule_embed / requires_pr_gate) as injected callables — the
# "costs programs_loader" seam — so the domain stays fastapi-free. The HTTP
# router injects ITS versions (which live in routers/tasks.py and pull fastapi
# for the test-seam machinery). The MCP surface MUST NOT import the router (that
# would drag fastapi into the collapsed single-process runtime), so it injects
# these MCP-local seams instead:
#
#   * mcp_sync_graph     -> graph_service.sync_task_to_graph (already fastapi-free,
#                           same KG node emit the HTTP surface does — no divergence).
#   * mcp_schedule_embed -> in-process auto-embed (S1 F4). Fires the SAME
#                           fastapi-free embed body the HTTP surface uses
#                           (embedding_service.embed_task_document) fire-and-forget
#                           on the running tool loop — no Node, no HTTP, no fork. The
#                           use_case calls this sync seam un-awaited (side effect),
#                           so it schedules a background task and returns immediately;
#                           the embed (incl. the slow model/remote backend call) runs OUTSIDE
#                           any write lock, then writes via the single-writer pool.
#                           No-ops gracefully when the embedder is unavailable.
#   * mcp_requires_pr_gate -> returns False for the MCP lifecycle helpers. The
#                           completed-PR gate is enforced by the PR workflow, while
#                           human approval remains in Console/Triage.
# Background task set (prevents GC of fire-and-forget embed coroutines) — the MCP
# process is the lifetime container, so this is a module-level set, the same pattern
# the HTTP router uses (routers.tasks._bg_embed_tasks). asyncio holds only a weak
# reference to a bare create_task() result, so an un-held task can be collected
# mid-flight; keeping it here until done prevents that.
_bg_embed_tasks: set[asyncio.Task] = set()


def mcp_schedule_embed(
    *,
    task_id: str,
    title: str,
    project: str,
    status: str,
    workspace_id: str,
    **_ignored: Any,
) -> None:
    """In-process auto-embed seam for the local MCP surface (S1 F4).

    Sync callable: the create/update task use_case invokes it un-awaited for its
    side effect. There is a running event loop when a tool calls it (tools are
    async), so we schedule the SHARED fastapi-free embed body
    (``embedding_service.embed_task_document``) fire-and-forget — the exact helper
    the HTTP router runs, just without FastAPI in the path. No-ops gracefully when
    the embedder is unavailable (mirrors the router's ``is_available()`` guard).
    """
    from core.api.services import embedding_service

    if not embedding_service.is_available():
        return

    async def _embed() -> None:
        try:
            await embedding_service.embed_task_document(
                task_id=task_id,
                title=title,
                project=project,
                status=status,
                workspace_id=workspace_id,
            )
        except Exception:
            logger.debug(
                "MCP auto-embed task %s failed (non-critical)", task_id, exc_info=True
            )

    try:
        t = asyncio.create_task(_embed())
    except RuntimeError:
        # No running loop (e.g. a sync caller outside an async context). Auto-embed
        # is a non-critical side effect; skip rather than crash the mutation.
        logger.debug("MCP auto-embed skipped: no running event loop")
        return
    _bg_embed_tasks.add(t)
    t.add_done_callback(_bg_embed_tasks.discard)


# Background set for the remote-backend fire-and-forget learning embeds (the local
# backend awaits inline instead — see below). Same GC-prevention pattern as
# ``_bg_embed_tasks``.
_bg_embed_learnings: set[asyncio.Task] = set()


def _mcp_learning_embed_inline_enabled() -> bool:
    """Whether MCP learning writes may await embedding on the response path."""
    return _env_flag("MARVIS_MCP_LEARNING_EMBED_INLINE", default=False)


def _mcp_learning_embed_inline_timeout_seconds() -> float:
    raw = os.environ.get("MARVIS_MCP_LEARNING_EMBED_INLINE_TIMEOUT_SECONDS")
    if raw is None or not raw.strip():
        return 2.0
    try:
        return max(float(raw), 0.1)
    except ValueError:
        logger.warning(
            "Invalid MARVIS_MCP_LEARNING_EMBED_INLINE_TIMEOUT_SECONDS=%r; using 2.0",
            raw,
        )
        return 2.0


async def mcp_embed_learning(
    *,
    learning_id: str,
    title: str,
    description: str,
    category: str,
    severity: str,
    prevention: str | None = None,
    project: str | None = None,
    workspace_id: str,
) -> None:
    """Backend-aware embed-on-write for learnings on the local MCP surface.

    The learning analogue of ``mcp_schedule_embed``. Hosted MCP must never keep the
    user-facing request open for embedding: slow remote providers, local fallback
    model loads, or DB contention can outlive the HTTP gateway and collapse the
    client transport. By default this schedules the shared fastapi-free embed body
    (``embedding_service.embed_learning_document``) fire-and-forget after the writer
    lock is released. Local installs that explicitly need synchronous immediate
    semantic retrieval can opt in with ``MARVIS_MCP_LEARNING_EMBED_INLINE=1``; that
    path is still bounded by ``MARVIS_MCP_LEARNING_EMBED_INLINE_TIMEOUT_SECONDS``.
    No-ops when the embedder is unavailable.
    """
    from core.api.services import embedding_service

    if not embedding_service.is_available():
        return

    async def _embed() -> None:
        try:
            await embedding_service.embed_learning_document(
                learning_id=learning_id,
                title=title,
                description=description,
                category=category,
                severity=severity,
                prevention=prevention,
                project=project,
                workspace_id=workspace_id,
            )
        except Exception:
            logger.debug(
                "MCP auto-embed learning %s failed (non-critical)",
                learning_id,
                exc_info=True,
            )

    if (
        embedding_service.embedding_is_synchronous()
        and _mcp_learning_embed_inline_enabled()
    ):
        timeout = _mcp_learning_embed_inline_timeout_seconds()
        try:
            await asyncio.wait_for(_embed(), timeout=timeout)
        except TimeoutError:
            logger.warning(
                "MCP inline learning embed %s timed out after %.1fs",
                learning_id,
                timeout,
            )
        return

    # Default hosted-safe path: fire-and-forget so create/update never blocks.
    try:
        t = asyncio.create_task(_embed())
    except RuntimeError:
        logger.debug("MCP auto-embed learning skipped: no running event loop")
        return
    _bg_embed_learnings.add(t)
    t.add_done_callback(_bg_embed_learnings.discard)


def mcp_requires_pr_gate(_project: str | None) -> bool:
    """Local PR-gate seam: governance gate collapses in single-user OSS (S1 §AUTH)."""
    return False


def set_tool_error_runtime(runtime: Literal["mcp", "fastmcp"]) -> None:
    """Select the ToolError class expected by the active FastMCP runtime."""
    global _TOOL_ERROR_RUNTIME
    _TOOL_ERROR_RUNTIME = runtime


def raise_mcp_error(err: ServiceError) -> NoReturn:
    """Surface a domain ``ServiceError`` as a real MCP tool error by RAISING.

    Raises ``mcp.server.fastmcp.exceptions.ToolError(f"{err.code}: {err.message}")``.
    FastMCP catches this in its ``call_tool`` path and returns a native
    ``CallToolResult(isError=True, content=[TextContent(text="<code>: <message>")])``
    — the SDK-native error channel a client (Claude Code) recognises as a tool
    error.

    Why RAISE and not return a dict (verified empirically, S1 F3.2): a *returned*
    value is success DATA — FastMCP places it in ``structuredContent`` with
    ``isError=False``, so a returned ``{"isError": True, ...}`` dict is silently a
    SUCCESS the client must manually inspect (it never trips the protocol flag).
    Worse, on the seven ``-> list[dict]`` tools that returned dict fails FastMCP's
    structured-output validation against the ``list`` type and the client gets a
    Pydantic validation error instead of the domain message. Raising fixes both:
    it emits a real ``isError`` for every tool AND bypasses output validation, so
    ``-> dict`` and ``-> list`` tools surface errors identically.

    The ``ToolError`` import is function-local so importing this module needs no
    ``mcp`` SDK (parity with ``server.py``).
    """
    if _TOOL_ERROR_RUNTIME == "fastmcp":
        from fastmcp.exceptions import ToolError
    else:
        from mcp.server.fastmcp.exceptions import ToolError

    suffix = ""
    if err.context:
        context = json.dumps(err.context, sort_keys=True, separators=(",", ":"))
        suffix = f" | {context}"
    raise ToolError(f"{err.code}: {err.message}{suffix}")


def dump(result: Any) -> Any:
    """Normalise a use_case return into an MCP-serialisable structure.

    Mutators return Pydantic DTOs (``.model_dump()``); read tools may already
    return plain dict/list. Lists of DTOs are mapped element-wise. This keeps the
    per-tool body a one-liner and centralises the DTO->dict decision (S1 F3
    return-typing rule: ``dict[str, Any]`` for heterogeneous reads, DTO dumps for
    mutators).
    """
    if hasattr(result, "model_dump"):
        return result.model_dump()
    if isinstance(result, list):
        return [r.model_dump() if hasattr(r, "model_dump") else r for r in result]
    return result
