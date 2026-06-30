# v2.0.0 - 2026-05-27 - S1 F1.7: thin adapter over use_cases.pull_requests (merge human-gate kept at Depends)
# v1.7.0 - 2026-03-11 - Fase 2: approve and request-changes endpoints (four-eyes gate)
"""HTTP adapter for the pull-requests domain (S1 collapse-runtime).

This router is a thin transport adapter. All branch/PR lifecycle + RBAC +
error-translation logic lives in :mod:`core.api.use_cases.pull_requests` (pure,
fastapi-free). Each handler resolves identity into a :class:`CallerContext`, calls
the use_case inside ``try/except`` -> ``to_http``, and owns the transport concerns.

MERGE HUMAN-GATE — kept at the ``Depends`` layer. ``POST /{task_id}/merge`` and
``/{task_id}/revert`` keep ``Depends(require_role(..., human_only=True))``.
``human_only=True`` swaps the auth dependency to cookie-only ``get_current_user``,
so a Bearer-only agent is rejected with ``401 Not authenticated`` upstream of the
handler (pinned by the merge-gate regression test ``test_br03_merge_pr_blocked_without_cookie``).
A use_case receiving a ``ctx`` cannot reproduce that 401, so the gate is NOT moved
into the domain — it stays exactly where it fires today.

ERROR BOUNDARIES kept in the adapter (legacy body parity):
  * ``MergeConflictError`` (raised inside ``pr_service`` -> propagates through the
    use_case) -> the exact legacy ``HTTPException(409, {message, conflicting_files})``
    dict body. ``to_http`` would drop ``conflicting_files``.
  * plain ``GitOpsError`` -> legacy ``HTTPException(500, str(exc))`` (pinned by
    ``tests/test_db_contention_task_flows.py::test_merge_push_failure_*`` which
    asserts the raw message in the 500 body). ``to_http``'s structured dict would
    break that substring assertion.
  * ``HTTPException``s raised inside ``pr_service`` (title/migration/push paths)
    propagate natively — Fase-2 conversion, out of scope here.
Domain :class:`ServiceError`s (404/409/422/403 the use_case raises) flow through
``to_http`` as the structured ``{code,message}`` body — their tests check status.

STAYS IN THE ADAPTER (transport concerns):
  * ``get_pull_request`` ``deep`` KG enrichment (rate-limit + access log +
    ``build_kg_context_for_pr``); ``core.api.services.kg.audit`` imports ``fastapi``
    so this MUST NOT touch the use_case (DECISION 2).
  * visibility resolution on ``get_merge_conflicts`` (``get_visible_projects`` needs
    ``UserInfo.teams``) — resolved here, passed in (DECISION 1).
  * approve/request-changes pass the full ``UserInfo`` (``user_id`` + team lookups)
    through to the use_case, which forwards it to ``pr_service``.

``MergeConflictResponse`` (response DTO) is imported from ``core.api.models``
(unchanged, like costs/tasks). REQUEST models stay defined here.
"""
from __future__ import annotations

import logging

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from core.api.db import get_db, get_write_db
from core.api.models import MergeConflictResponse, UserInfo
from core.api.rbac import require_role
from core.api.routers._adapter import to_http
from core.api.security import get_current_user_or_agent, require_any_auth
from core.api.services.kg.audit import check_deep_rate_limit, log_kg_deep_access
from core.api.services.kg.lens import build_kg_context_for_pr
from core.api.use_cases import pull_requests as uc
from core.api.use_cases._context import CallerContext
from core.api.use_cases._errors import ServiceError
from core.api.visibility import get_visible_projects

# Re-export the git-ops error classes from the use_case so existing importers of
# ``core.api.routers.pull_requests.GitOpsError`` / ``MergeConflictError`` keep
# working unchanged.
from core.api.use_cases.pull_requests import (  # noqa: F401  (re-export surface)
    GitOpsError,
    MergeConflictError,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/pull_requests", tags=["pull-requests"])


# --- Request models (kept in the adapter) ---

class SubmitPrRequest(BaseModel):
    title: str
    body: str = ""

class ClosePrRequest(BaseModel):
    reason: str = ""

class UpdatePrRequest(BaseModel):
    title: str | None = None
    body: str | None = None

class RegisterBranchRequest(BaseModel):
    branch_name: str
    worktree_path: str | None = None

class RequestChangesBody(BaseModel):
    comment: str


# --- Endpoints (thin adapters) ---

@router.get("/merge-conflicts", response_model=MergeConflictResponse)
async def get_merge_conflicts(
    project: str = Query(..., description="Project slug to check for migration conflicts"),
    user: UserInfo = Depends(require_any_auth),
    db: aiosqlite.Connection = Depends(get_db),
):
    """Detect migration number conflicts across open PRs for a project."""
    # DECISION 1: resolve visibility at the boundary, enforce in the use_case
    # (404 on a non-visible slug — does not reveal existence).
    visible_projects = await get_visible_projects(db, user)
    ctx = CallerContext.from_user_info(user, is_human_session=False)
    try:
        conflicts = await uc.get_merge_conflicts(
            ctx, db, project=project, visible_projects=visible_projects
        )
        return MergeConflictResponse(conflicts=conflicts)
    except ServiceError as e:
        raise to_http(e)
    except GitOpsError as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/{task_id}/branch")
async def create_branch(
    task_id: str,
    user: UserInfo = Depends(require_role("operator", "admin", "super_admin")),
    db: aiosqlite.Connection = Depends(get_db),
):
    """Create git worktree and branch for a task. Idempotent."""
    ctx = CallerContext.from_user_info(user, is_human_session=False)
    try:
        return await uc.create_branch(ctx, db, task_id=task_id)
    except ServiceError as e:
        raise to_http(e)
    except GitOpsError as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/{task_id}/register")
async def register_branch(
    task_id: str,
    body: RegisterBranchRequest,
    user: UserInfo = Depends(require_role("operator", "admin", "super_admin")),
    db: aiosqlite.Connection = Depends(get_write_db),
):
    """Register an externally-created branch as a draft PR record.

    Used when an agent creates a worktree via git directly (not via /branch endpoint).
    Idempotent: returns existing active PR if one already exists.
    """
    ctx = CallerContext.from_user_info(user, is_human_session=False)
    try:
        return await uc.register_branch(
            ctx, db, task_id=task_id, branch_name=body.branch_name,
            worktree_path=body.worktree_path,
        )
    except ServiceError as e:
        raise to_http(e)


@router.get("/{task_id}")
async def get_pull_request(
    task_id: str,
    deep: bool = Query(False),
    user: UserInfo = Depends(get_current_user_or_agent),
    db: aiosqlite.Connection = Depends(get_db),
):
    """Get PR status and diff for a task."""
    ctx = CallerContext.from_user_info(user, is_human_session=False)
    try:
        result = await uc.get_pull_request(ctx, db, task_id=task_id)
    except ServiceError as e:
        raise to_http(e)

    # DECISION 2: deep KG enrichment is a per-surface adapter concern.
    if deep:
        check_deep_rate_limit(user.username)
        log_kg_deep_access(user.username, "get_pull_request", task_id)
        result["kg_context"] = await build_kg_context_for_pr(db, task_id, deep=True)
    return result


@router.post("/{task_id}/submit")
async def submit_pull_request(
    task_id: str,
    body: SubmitPrRequest,
    user: UserInfo = Depends(require_role("operator", "admin", "super_admin")),
    db: aiosqlite.Connection = Depends(get_write_db),
):
    """Submit branch for review. Moves PR from draft to open."""
    ctx = CallerContext.from_user_info(user, is_human_session=False)
    try:
        return await uc.submit_pull_request(
            ctx, db, task_id=task_id, title=body.title, body=body.body
        )
    except ServiceError as e:
        raise to_http(e)
    except GitOpsError as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/{task_id}/merge")
async def merge_pull_request(
    task_id: str,
    user: UserInfo = Depends(require_role("operator", "admin", "super_admin", human_only=True)),
    db: aiosqlite.Connection = Depends(get_write_db),
):
    """Merge PR. Human operator+ authorization required (no agent tokens).

    The human-only gate is the ``human_only=True`` dependency above (cookie-only
    auth) — it fires upstream of this handler and is NOT duplicated in the use_case.
    """
    ctx = CallerContext.from_user_info(user, is_human_session=True)
    try:
        return await uc.merge_pull_request(ctx, db, task_id=task_id)
    except MergeConflictError as exc:
        # Preserve the exact legacy 409 dict body (message + conflicting_files).
        raise HTTPException(
            status_code=409,
            detail={
                "message": (
                    "Merge conflict: cannot fast-forward or 3-way merge the PR branch into main. "
                    "Reason: main has changes that touch the same lines as this PR since the branch diverged. "
                    "Fix: (1) checkout the PR branch in its worktree, (2) `git fetch origin && git rebase origin/main`, "
                    "(3) resolve conflicts in the files listed below, (4) push the rebased branch, (5) retry merge."
                ),
                "conflicting_files": exc.conflicting_files,
            },
        )
    except GitOpsError as exc:
        # Plain GitOpsError -> legacy 500 plain-string body.
        raise HTTPException(status_code=500, detail=str(exc))
    except ServiceError as e:
        raise to_http(e)


@router.post("/{task_id}/approve")
async def approve_pull_request(
    task_id: str,
    user: UserInfo = Depends(require_role("admin", "super_admin", "operator")),
    db: aiosqlite.Connection = Depends(get_write_db),
):
    """Approva PR. Richiede team_admin del progetto o admin+. Four-eyes: non puoi approvare la tua PR."""
    ctx = CallerContext.from_user_info(user, is_human_session=False)
    try:
        return await uc.approve_pull_request(ctx, db, task_id=task_id, reviewer=user)
    except ServiceError as e:
        raise to_http(e)


@router.post("/{task_id}/request-changes")
async def request_pr_changes(
    task_id: str,
    body: RequestChangesBody,
    user: UserInfo = Depends(require_role("admin", "super_admin", "operator")),
    db: aiosqlite.Connection = Depends(get_write_db),
):
    """Request changes. Revoca approvazione e rimanda task in_progress."""
    ctx = CallerContext.from_user_info(user, is_human_session=False)
    try:
        return await uc.request_pull_request_changes(
            ctx, db, task_id=task_id, reviewer=user, comment=body.comment
        )
    except ServiceError as e:
        raise to_http(e)


@router.post("/{task_id}/close")
async def close_pull_request(
    task_id: str,
    body: ClosePrRequest,
    user: UserInfo = Depends(require_role("operator", "admin", "super_admin")),
    db: aiosqlite.Connection = Depends(get_write_db),
):
    """Close PR without merge. Task returns to in_progress."""
    ctx = CallerContext.from_user_info(user, is_human_session=False)
    try:
        return await uc.close_pull_request(ctx, db, task_id=task_id, reason=body.reason)
    except ServiceError as e:
        raise to_http(e)


@router.patch("/{task_id}")
async def update_pull_request(
    task_id: str,
    body: UpdatePrRequest,
    user: UserInfo = Depends(require_role("operator", "admin", "super_admin")),
    db: aiosqlite.Connection = Depends(get_write_db),
):
    """Update PR title or body."""
    ctx = CallerContext.from_user_info(user, is_human_session=False)
    try:
        return await uc.update_pull_request(
            ctx, db, task_id=task_id, title=body.title, body=body.body
        )
    except ServiceError as e:
        raise to_http(e)


@router.post("/{task_id}/revert")
async def revert_pull_request(
    task_id: str,
    user: UserInfo = Depends(require_role("operator", "admin", "super_admin", human_only=True)),
    db: aiosqlite.Connection = Depends(get_write_db),
):
    """Revert a merged PR. Creates a new task + new PR with git revert commit.
    Human operator+ authorization required (cookie-only ``human_only`` dependency)."""
    ctx = CallerContext.from_user_info(user, is_human_session=True)
    try:
        return await uc.revert_pull_request(ctx, db, task_id=task_id)
    except ServiceError as e:
        raise to_http(e)
    except GitOpsError as exc:
        raise HTTPException(status_code=500, detail=str(exc))
