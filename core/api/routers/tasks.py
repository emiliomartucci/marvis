# v3.0.0 - 2026-05-27 - S1 F1.6: thin adapter over use_cases.tasks (CORNERSTONE — human-approval gate)
# v2.7.0 - 2026-04-22 - Anti-zombie C: zombie-scan + bulk-reject endpoints (weekly aging cleanup)
# v2.6.0 - 2026-04-17 - KG bug fix: sync new task to graph_nodes on POST /tasks (closes handoff orphan_reason=task_id_not_in_graph)
# v2.5.0 - 2026-04-12 - Add stale-reset endpoint for REM HYGIENE step
"""HTTP adapter for the tasks domain (S1 collapse-runtime CORNERSTONE).

This router is a thin transport adapter. All CRUD/validation/RBAC/visibility/
transition logic — including the human-approval four-eyes gate — lives in
:mod:`core.api.use_cases.tasks` (pure, fastapi-free). Each handler resolves
identity into a :class:`CallerContext`, calls the use_case inside
``try/except ServiceError`` -> ``to_http``, and owns the transport concerns.

CORNERSTONE — the human-approval gate. The use_case checks ``ctx.is_human_session``
and raises ``AuthorizationError(code="approval_requires_human")``. This adapter
fills ``is_human_session`` from the ``pir_session`` cookie and re-raises that ONE
error as the legacy PLAIN-STRING 403 (``detail`` is a string, not a ``{code,message}``
dict), preserving the HTTP contract pinned by
``tests/test_tasks.py::test_pending_to_approved_requires_cookie_auth``. Likewise
the transition + completion guards (``invalid transition`` 422,
``review-needs-PR`` 422, ``completed-needs-no-open-PR`` 422,
``completed-needs-merged-PR`` 422) keep the legacy PLAIN-STRING detail via
``_transition_to_http`` below, so the existing ``"merged pr workflow"`` substring
assertion keeps matching. Non-transition errors (404 not-found, 409 duplicate /
delete-in-progress, 403 insufficient-permissions) flow through ``to_http`` as the
structured ``{code,message}`` body — their tests only check the status code.

STAYS IN THE ADAPTER (transport concerns):
  * ``_check_rate_limit`` (in-memory per-process 429 guard) — called before delegating.
  * ``get_task`` ``deep`` KG enrichment (rate-limit + access log + ``kg_context``)
    and the ``list_tasks`` ``deep_requires_filter`` 400 guard (DECISION 2/3).
  * session-owner resolution (``resolve_session_owner`` reads ``X-Session-Name``)
    and visibility resolution (``get_visible_projects``) — resolved here, passed in.
  * ``POST /{id}/cost-entries`` keeps its cookie-only ``Depends(get_current_user)``.

CALLABLE SEAMS kept in this module so existing import/monkeypatch tests stay valid
and the use_case stays fastapi-free:
  * ``sync_task_to_graph`` — imported here and passed into ``create_task`` (the
    create-path source references the symbol; the update-path never does).
  * ``_schedule_embed_task`` + ``_bg_embed_tasks`` — defined here; tests call
    ``tasks_router._schedule_embed_task`` directly.
  * ``_project_requires_pr_gate`` — defined here; passed into ``update_task`` at
    call time so ``monkeypatch.setattr(tasks_router, "_project_requires_pr_gate", ...)``
    takes effect.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException, Query, Request

from core.api.config import settings
from core.api.db import get_db, get_write_db
from core.api.models import (
    HumanCostEntryCreate,
    ProjectSummary,
    TaskCostSummary,
    TaskCreateRequest,
    TaskDetailResponse,
    TaskSummary,
    TaskUpdateRequest,
    UserInfo,
)
from core.api.rbac import require_role
from core.api.routers._adapter import to_http
from core.api.security import (
    get_current_user,
    get_current_user_or_agent,
    resolve_session_owner,
)
from core.api.services.graph_service import sync_task_to_graph
from core.api.services.kg.audit import check_deep_rate_limit, log_kg_deep_access
from core.api.services.kg.lens import build_kg_context_for_task
from core.api.use_cases import tasks as uc
from core.api.use_cases._context import CallerContext
from core.api.use_cases._errors import AuthorizationError, ServiceError, ValidationError
from core.api.visibility import get_visible_projects

# Re-export the moved DTOs + pure helpers + constants from the use_case so that
# (a) `response_model=` below references the same classes and (b) existing
# importers keep working unchanged.
from core.api.use_cases.tasks import (  # noqa: F401  (re-export surface)
    ZOMBIE_THRESHOLD_DAYS_DEFAULT,
    BulkRejectRequest,
    BulkRejectResponse,
    ZombieScanResponse,
    _base_task_fields,
    _PR_STATUS_SUBQUERY,
    _row_to_task,
    _row_to_task_list,
)

logger = logging.getLogger(__name__)

# Background task set (prevents GC). Kept in the adapter — task auto-embedding is a
# per-surface concern and `tests/test_task_auto_embedding.py` references
# `tasks_router._bg_embed_tasks` / `tasks_router._schedule_embed_task` directly.
_bg_embed_tasks: set[asyncio.Task] = set()


def _schedule_embed_task(
    task_id: str, title: str, project: str, status: str, workspace_id: str
) -> None:
    """Fire-and-forget: embed task in background. Silently no-ops if embedder unavailable.

    Thin HTTP-surface seam: the embed body itself lives in the fastapi-free
    ``embedding_service.embed_task_document`` (the SAME helper the MCP surface calls
    via ``mcp._adapter.mcp_schedule_embed`` — no fork, S1 F4). This wrapper keeps its
    name/signature/module + ``_bg_embed_tasks`` set because
    ``tests/test_task_auto_embedding.py`` references them directly.
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
                "Auto-embed task %s failed (non-critical)", task_id, exc_info=True
            )

    t = asyncio.create_task(_embed())
    _bg_embed_tasks.add(t)
    t.add_done_callback(_bg_embed_tasks.discard)


def _project_requires_pr_gate(project_slug: str | None) -> bool:
    """Code/system projects must close through the PR workflow, not direct task completion."""
    if not project_slug:
        return False
    try:
        from core.api.routers.projects import _find_project_entry

        entry = _find_project_entry(project_slug)
    except Exception:
        logger.warning(
            "Failed to resolve project type for %s", project_slug, exc_info=True
        )
        return False
    return bool(entry and entry.project_type in {"code", "system"})


router = APIRouter(prefix="/api/v1/tasks", tags=["tasks"])

# In-memory rate limiter: token -> list of timestamps
_rate_limits: dict[str, list[float]] = defaultdict(list)
_RATE_LIMITS_MAX_KEYS = 1000


def _check_rate_limit(identity: str) -> None:
    """Simple sliding window rate limiter (in-memory, per-process)."""
    now = time.time()
    window = 60.0
    max_requests = settings.tasks_rate_limit_per_min

    timestamps = _rate_limits[identity]
    # Remove old entries
    _rate_limits[identity] = [t for t in timestamps if now - t < window]
    if len(_rate_limits[identity]) >= max_requests:
        raise HTTPException(
            status_code=429,
            detail=(
                f"Rate limit exceeded for '{identity}': {max_requests} requests/minute. "
                "Reason: per-identity sliding window to prevent runaway loops. "
                "Fix: wait 60s before retrying, or batch operations (e.g. fetch task lists once, cache in memory). "
                "If you need a higher limit, ask an admin to bump settings.tasks_rate_limit_per_min."
            ),
        )
    _rate_limits[identity].append(now)
    # Evict stale entries periodically
    if len(_rate_limits) > _RATE_LIMITS_MAX_KEYS:
        stale = [k for k, v in _rate_limits.items() if not v or (now - v[-1]) > window]
        for k in stale:
            del _rate_limits[k]


def _transition_to_http(err: ValidationError) -> HTTPException:
    """Re-raise a transition/guard ValidationError as the legacy PLAIN-STRING body.

    The original router raised ``HTTPException(422, detail="<plain string>")`` for
    transition + completion-guard errors. Existing tests assert on that string
    (e.g. ``"merged pr workflow" in detail.lower()``), so for this family we keep
    the detail as a plain string rather than ``to_http``'s ``{code,message}`` dict.
    """
    return HTTPException(status_code=err.http_status, detail=err.message)


# ---------------------------------------------------------------------------
# Endpoints (thin adapters)
# ---------------------------------------------------------------------------


@router.get("/summary")
async def get_tasks_summary(
    user: UserInfo = Depends(get_current_user_or_agent),
    db: aiosqlite.Connection = Depends(get_db),
) -> TaskSummary:
    """Cross-project task summary."""
    _check_rate_limit(user.username)
    ctx = CallerContext.from_user_info(user, is_human_session=False)
    try:
        return await uc.get_tasks_summary(ctx, db)
    except ServiceError as e:
        raise to_http(e)


@router.get("/projects")
async def get_task_projects(
    user: UserInfo = Depends(get_current_user_or_agent),
    db: aiosqlite.Connection = Depends(get_db),
) -> list[ProjectSummary]:
    """List projects with task counts."""
    _check_rate_limit(user.username)
    ctx = CallerContext.from_user_info(user, is_human_session=False)
    try:
        return await uc.get_task_projects(ctx, db)
    except ServiceError as e:
        raise to_http(e)


@router.post("", status_code=201)
async def create_task(
    body: TaskCreateRequest,
    request: Request,
    user: UserInfo = Depends(require_role("operator", "admin", "super_admin")),
    db: aiosqlite.Connection = Depends(get_write_db),
) -> TaskDetailResponse:
    """Create a new task."""
    _check_rate_limit(user.username)

    # Session-based attribution: when an MCP agent sends X-Session-Name, attribute
    # the task to the session's human owner instead of the agent identity. This is
    # a transport concern (reads `request`), resolved here and passed in.
    created_by_value = user.username
    session_owner = await resolve_session_owner(request, db)
    if session_owner:
        created_by_value = session_owner

    ctx = CallerContext.from_user_info(user, is_human_session=False)
    try:
        return await uc.create_task(
            ctx,
            db,
            body=body,
            created_by=created_by_value,
            sync_graph=sync_task_to_graph,
            schedule_embed=_schedule_embed_task,
        )
    except ServiceError as e:
        raise to_http(e)


@router.get("", response_model=list[TaskDetailResponse])
async def list_tasks(
    project: str | None = Query(None, pattern=r"^[a-z0-9][a-z0-9_.\-]{0,126}$"),
    status: str | None = None,
    kind: str | None = Query(None, pattern=r"^(normal|idea)$"),
    priority: str | None = None,
    created_by: str | None = None,
    owner_id: str | None = None,
    delegation: str | None = None,
    tags: str | None = Query(None, description="Comma-separated tags (OR logic)"),
    limit: int = Query(20, ge=1, le=500),
    offset: int = Query(0, ge=0),
    sort: str = Query("created_at:desc"),
    include_deleted: bool = False,
    detailed: bool = Query(False, description="Include description and comments"),
    deep: bool = Query(False),
    user: UserInfo = Depends(get_current_user_or_agent),
    db: aiosqlite.Connection = Depends(get_db),
) -> list[TaskDetailResponse]:
    """List tasks with filters."""
    _check_rate_limit(user.username)

    # DECISION 3: deep_requires_filter is an adapter-owned guard tied to the
    # adapter-owned `deep` feature. Keep the exact 400 (NOT via ServiceError).
    if deep and not project:
        raise HTTPException(
            status_code=400,
            detail="deep=true requires ?project=<slug> filter (aggregate KG context needs a project scope)",
        )

    # DECISION 1 (the visibility template): resolve visibility at the boundary
    # (needs UserInfo.teams, not carried by CallerContext) and pass it in.
    visible_projects = await get_visible_projects(db, user)

    ctx = CallerContext.from_user_info(user, is_human_session=False)
    try:
        return await uc.list_tasks(
            ctx,
            db,
            project=project,
            status=status,
            kind=kind,
            priority=priority,
            created_by=created_by,
            owner_id=owner_id,
            delegation=delegation,
            tags=tags,
            limit=limit,
            offset=offset,
            sort=sort,
            include_deleted=include_deleted,
            detailed=detailed,
            visible_projects=visible_projects,
        )
    except ServiceError as e:
        raise to_http(e)


@router.get("/{task_id}", response_model=TaskDetailResponse)
async def get_task(
    task_id: str,
    deep_param: bool | None = Query(None, alias="deep"),
    user: UserInfo = Depends(get_current_user_or_agent),
    db: aiosqlite.Connection = Depends(get_db),
) -> TaskDetailResponse:
    """Get a single task by ID."""
    _check_rate_limit(user.username)

    # DECISION 1: resolve visibility at the boundary, enforce in the use_case.
    visible_projects = await get_visible_projects(db, user)

    ctx = CallerContext.from_user_info(user, is_human_session=False)
    try:
        result = await uc.get_task(
            ctx, db, task_id=task_id, visible_projects=visible_projects
        )
    except ServiceError as e:
        raise to_http(e)

    # DECISION 2: deep KG enrichment is a per-surface adapter concern.
    deep = deep_param if deep_param is not None else settings.kg_http_deep_default
    deep_source = "client" if deep_param is not None else "env"
    if deep:
        check_deep_rate_limit(user.username)
        log_kg_deep_access(user.username, "get_task", task_id)
        result.kg_context = await build_kg_context_for_task(db, task_id, deep=True)
        if result.kg_context and "meta" in result.kg_context:
            result.kg_context["meta"]["deep_effective"] = deep
            result.kg_context["meta"]["deep_default_source"] = deep_source
    return result


@router.patch("/{task_id}", response_model=TaskDetailResponse)
async def update_task(
    task_id: str,
    body: TaskUpdateRequest,
    request: Request,
    user: UserInfo = Depends(require_role("operator", "admin", "super_admin")),
    db: aiosqlite.Connection = Depends(get_write_db),
) -> TaskDetailResponse:
    """Update a task. Validates status transitions."""
    _check_rate_limit(user.username)

    # CORNERSTONE: the cookie is the only transport signal for "human session".
    # Fill ctx.is_human_session here; the use_case decides the four-eyes gate.
    ctx = CallerContext.from_user_info(
        user, is_human_session=bool(request.cookies.get("pir_session"))
    )
    try:
        return await uc.update_task(
            ctx,
            db,
            task_id=task_id,
            body=body,
            requires_pr_gate=_project_requires_pr_gate,
            schedule_embed=_schedule_embed_task,
        )
    except AuthorizationError as e:
        if e.code == "approval_requires_human":
            # Preserve the legacy plain-string 403 body (HTTP contract parity).
            raise HTTPException(status_code=403, detail=uc.APPROVAL_REQUIRES_HUMAN_DETAIL)
        raise to_http(e)
    except ValidationError as e:
        # Transition + completion guards keep the legacy plain-string 422 detail.
        raise _transition_to_http(e)
    except ServiceError as e:
        raise to_http(e)


@router.get("/{task_id}/cost-entries", response_model=TaskCostSummary)
async def get_task_cost_entries(
    task_id: str,
    user: UserInfo = Depends(get_current_user_or_agent),
    db: aiosqlite.Connection = Depends(get_db),
) -> TaskCostSummary:
    """Return cost summary + all entries for a task."""
    ctx = CallerContext.from_user_info(user, is_human_session=False)
    try:
        return await uc.get_task_cost_entries(ctx, db, task_id=task_id)
    except ServiceError as e:
        raise to_http(e)


@router.post("/{task_id}/cost-entries", response_model=TaskCostSummary, status_code=201)
async def create_human_cost_entry(
    task_id: str,
    body: HumanCostEntryCreate,
    user: UserInfo = Depends(get_current_user),
    db: aiosqlite.Connection = Depends(get_write_db),
) -> TaskCostSummary:
    """Manually record human time for a task. Cookie auth required."""
    _check_rate_limit(user.username)
    # Cookie-only auth: get_current_user already enforces a human session.
    ctx = CallerContext.from_user_info(user, is_human_session=True)
    try:
        return await uc.create_human_cost_entry(ctx, db, task_id=task_id, body=body)
    except ServiceError as e:
        raise to_http(e)


@router.delete("/{task_id}", status_code=204)
async def delete_task(
    task_id: str,
    user: UserInfo = Depends(require_role("admin", "super_admin")),
    db: aiosqlite.Connection = Depends(get_write_db),
) -> None:
    """Soft delete a task. Cannot delete in_progress tasks."""
    _check_rate_limit(user.username)
    ctx = CallerContext.from_user_info(user, is_human_session=False)
    try:
        await uc.delete_task(ctx, db, task_id=task_id)
    except ServiceError as e:
        raise to_http(e)


@router.post("/reminders/check", status_code=200)
async def trigger_reminder_check(
    user: UserInfo = Depends(require_role("operator")),
) -> dict:
    """Trigger manual reminder check. Called by cron or agent."""
    ctx = CallerContext.from_user_info(user, is_human_session=False)
    try:
        return await uc.trigger_reminder_check(ctx)
    except ServiceError as e:
        raise to_http(e)


@router.post("/stale-reset", status_code=200)
async def reset_stale_tasks(
    stale_days: int = Query(
        7, ge=1, le=90, description="Days without update to consider stale"
    ),
    user: UserInfo = Depends(require_role("operator", "admin", "super_admin")),
    db: aiosqlite.Connection = Depends(get_write_db),
) -> dict:
    """Reset stale in_progress tasks back to approved.

    Finds tasks with status='in_progress' and updated_at older than
    stale_days ago, resets them to 'approved', and adds a 'stale_reset' tag.
    Called by REM in its HYGIENE step.
    """
    _check_rate_limit(user.username)
    ctx = CallerContext.from_user_info(user, is_human_session=False)
    try:
        return await uc.reset_stale_tasks(ctx, db, stale_days=stale_days)
    except ServiceError as e:
        raise to_http(e)


@router.post("/zombie-scan", response_model=ZombieScanResponse)
async def zombie_scan(
    threshold_days: int = Query(
        ZOMBIE_THRESHOLD_DAYS_DEFAULT,
        ge=7,
        le=365,
        description="Days without update to flag a task as zombie (default 21)",
    ),
    dry_run: bool = Query(
        False, description="If true, scan only — no notifications written"
    ),
    user: UserInfo = Depends(require_role("operator", "admin", "super_admin")),
    db: aiosqlite.Connection = Depends(get_write_db),
) -> ZombieScanResponse:
    """Scan for aging zombie tasks and emit one Console notification per project.

    Zombie = approved + updated_at < now - threshold_days + no open/draft/merging PR
    + no 'dormant-ok' tag. One notification per project aggregates the task list.

    Called by weekly systemd user timer (`marvisx-zombie-detect.timer`) or ad-hoc
    from Console. dry_run=true returns the report without writing notifications.
    """
    _check_rate_limit(user.username)
    ctx = CallerContext.from_user_info(user, is_human_session=False)
    try:
        return await uc.zombie_scan(
            ctx, db, threshold_days=threshold_days, dry_run=dry_run
        )
    except ServiceError as e:
        raise to_http(e)


@router.post("/bulk-reject", response_model=BulkRejectResponse)
async def bulk_reject_tasks(
    body: BulkRejectRequest,
    user: UserInfo = Depends(require_role("admin", "super_admin")),
    db: aiosqlite.Connection = Depends(get_write_db),
) -> BulkRejectResponse:
    """Bulk-reject multiple tasks with a reason. Admin-only.

    Use case: weekly zombie cleanup triggered from Console notification.
    Validates each transition via VALID_TRANSITIONS (approved/pending/failed
    → rejected). Logs one audit entry per successful rejection plus a summary
    audit entry for the batch. Partial failures are reported, not raised.
    """
    _check_rate_limit(user.username)
    ctx = CallerContext.from_user_info(user, is_human_session=False)
    try:
        return await uc.bulk_reject_tasks(
            ctx, db, task_ids=body.task_ids, reason=body.reason
        )
    except ServiceError as e:
        raise to_http(e)
