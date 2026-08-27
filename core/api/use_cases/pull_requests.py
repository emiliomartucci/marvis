# v1.0.0 - 2026-05-27 - S1 F1.7: pull_requests use_cases extracted from router (merge human-gate)
"""Pull-requests use_cases — pure domain logic, transport-agnostic (no ``fastapi``).

Sibling of :mod:`core.api.use_cases.learnings` (the S1 "collapse runtime"
TEMPLATE). One pure async function per operation, signature
``(ctx: CallerContext, db, *typed_args) -> <DTO/dict>``. The HTTP router becomes a
thin adapter that resolves identity into a :class:`CallerContext`, calls these
functions, and maps :class:`ServiceError` -> ``HTTPException`` via
``routers/_adapter.to_http``. The Python MCP surface calls the SAME
functions with an explicit MCP ``CallerContext`` from the adapter. One
implementation, no fork.

How the template decisions land on the PR domain:

MERGE/REVERT HUMAN-GATE — defense in depth across transport and domain.
    HTTP keeps ``Depends(require_role(..., human_only=True))`` for early rejection.
    The use cases also call :func:`has_approval_authority`, because direct/internal
    callers must not gain destructive PR authority by constructing an operator
    ``CallerContext``. A validated human/local session passes directly; an agent
    passes only through a persisted, active, bounded operator+ delegation. There
    is still no MCP Git/PR lifecycle tool.

DECISION 1 — Visibility on ``get_merge_conflicts``. The legacy endpoint called
    ``check_project_access(project, user, db)`` (raises 404 if the project is not
    visible — does not reveal existence). ``UserInfo.teams`` is NOT carried by
    ``CallerContext``, so the ADAPTER resolves ``visible_projects`` and passes it
    in; this use_case enforces it by raising :class:`NotFoundError` (same 404,
    same ``"Not found"`` shape) when the project is outside the visible set.
    ``visible_projects=None`` means "no restriction" (admin/agent or MCP/local).

DECISION 2 — ``deep`` KG enrichment on ``get_pull_request`` is a per-surface
    adapter concern (rate-limit + access log + ``build_kg_context_for_pr``). It
    also depends on ``core.api.services.kg.audit`` which imports ``fastapi`` at
    module top, so it MUST NOT touch this module. ``get_pull_request`` returns the
    PR status dict as-is; the adapter attaches ``kg_context`` when ``deep``.

SERVICE IMPORTS ARE FUNCTION-LOCAL (the search.py pattern). ``pr_service`` imports
    ``fastapi.HTTPException`` (a Fase-2 conversion target, OUT OF SCOPE here), so
    importing it at module top would pull ``fastapi`` into ``use_cases`` and break
    the import-linter ``use_cases-no-fastapi`` contract. Every use_case imports
    ``pr_service`` (and ``services.audit.log_audit``) INSIDE the function body.
    ``HTTPException``s raised inside ``pr_service`` (the title/migration/push paths)
    propagate natively over HTTP for now — correct behavior; their conversion to
    ``ServiceError`` is Fase 2 and explicitly not done here.

ERROR TRANSLATION. The errors the ROUTER ITSELF raised (catching ``pr_service``'s
    ``ValueError`` / ``PermissionError`` / ``GitOpsError`` / ``MergeConflictError``)
    become domain :class:`ServiceError` subclasses here with the SAME status:
      * ``ValueError`` -> ``ConflictError`` (409) on ``create_branch`` /
        ``NotFoundError`` (404) on ``get_pull_request`` / ``ValidationError`` (422)
        elsewhere — matching each endpoint's original status verbatim.
      * ``PermissionError`` -> ``AuthorizationError`` (403) on approve/request-changes.
      * ``MergeConflictError`` -> ``MergeConflictError`` is re-raised UNCHANGED so
        the adapter can build the exact legacy 409 dict body (``message`` +
        ``conflicting_files``) — ``to_http`` only emits ``{code,message}`` and would
        drop ``conflicting_files``.
      * plain ``GitOpsError`` -> re-raised UNCHANGED so the adapter maps it to the
        legacy ``HTTPException(500, str(exc))`` (pinned by
        ``tests/test_db_contention_task_flows.py::test_merge_push_failure_*`` which
        asserts the raw message text in the 500 body). ``to_http``'s structured
        dict would break that substring assertion.
    ``MergeConflictError`` / ``GitOpsError`` come from ``services.git_ops`` which is
    fastapi-free, so they are imported at module top (safe).

``MergeConflictResponse`` (response DTO) lives in ``core.api.models`` and is NOT
moved (like costs/tasks). The router keeps the REQUEST models.
"""
from __future__ import annotations

import logging
import uuid

import aiosqlite

from core.api.services.git_ops import GitOpsError, MergeConflictError
from core.api.use_cases._context import (
    ApprovalAuthorityReceipt,
    CallerContext,
    require_role_ctx,
    require_workspace_ctx,
    resolve_approval_authority,
)
from core.api.use_cases._errors import (
    AuthorizationError,
    ConflictError,
    NotFoundError,
    ServiceError,
    ValidationError,
)

logger = logging.getLogger(__name__)

PR_LIFECYCLE_REQUIRES_HUMAN_DETAIL = (
    "PR merge and revert require a validated human session or an active "
    "persisted delegation"
)


async def _require_pr_lifecycle_authority(
    ctx: CallerContext, db: aiosqlite.Connection
) -> ApprovalAuthorityReceipt:
    """Keep destructive PR authority in the domain, not only the transport."""
    authority = await resolve_approval_authority(ctx, db)
    if authority is None:
        raise AuthorizationError(
            code="pr_lifecycle_requires_human",
            message=PR_LIFECYCLE_REQUIRES_HUMAN_DETAIL,
        )
    return authority


async def _append_pr_lifecycle_audit(
    ctx: CallerContext,
    db: aiosqlite.Connection,
    *,
    operation: str,
    stage: str,
    task_id: str,
    correlation_id: str,
    authority: ApprovalAuthorityReceipt,
    failure_type: str | None = None,
) -> None:
    """Persist one durable saga receipt around an external Git side effect."""
    from core.api.services.audit import log_audit

    if db.in_transaction:
        raise RuntimeError("PR lifecycle audit requires a clean transaction boundary")
    details = {
        "task_id": task_id,
        "stage": stage,
        "correlation_id": correlation_id,
        **authority.audit_details(),
    }
    if failure_type is not None:
        details["failure_type"] = failure_type
    action = f"pr.{operation}" if stage == "confirmed" else f"pr.{operation}.{stage}"
    workspace_id = require_workspace_ctx(ctx)
    try:
        await db.execute("BEGIN IMMEDIATE")
        await log_audit(
            db,
            action=action,
            user=ctx.username,
            resource_type="pull_request",
            resource_id=task_id,
            details=details,
            workspace_id=workspace_id,
        )
        await db.commit()
    except Exception:
        await db.rollback()
        raise


# ---------------------------------------------------------------------------
# Use cases — reads
# ---------------------------------------------------------------------------


async def get_merge_conflicts(
    ctx: CallerContext,
    db: aiosqlite.Connection,
    *,
    project: str,
    visible_projects: set[str] | None = None,
) -> list[dict]:
    """Detect migration-number conflicts across open PRs for a project.

    Any authenticated caller. Visibility (DECISION 1): the adapter resolves
    ``visible_projects``; this use_case raises :class:`NotFoundError` (404, same
    ``"Not found"`` shape as the legacy ``check_project_access``) when ``project``
    is outside the visible set. ``None`` is unrestricted.

    Raises :class:`ValidationError` (422) on a bad project arg and re-raises
    :class:`GitOpsError` (-> 500 in the adapter) — matching the legacy router.
    """
    workspace_id = require_workspace_ctx(ctx)
    if visible_projects is not None and project not in visible_projects:
        raise NotFoundError(code="project_not_found", message="Not found")

    from core.api.services import pr_service

    try:
        return await pr_service.get_merge_conflicts(
            project, db, workspace_id=workspace_id
        )
    except ValueError as exc:
        raise ValidationError(code="invalid_merge_conflicts_request", message=str(exc))
    # GitOpsError propagates; the adapter maps it to the legacy 500.


async def get_pull_request(
    ctx: CallerContext,
    db: aiosqlite.Connection,
    *,
    task_id: str,
    visible_projects: set[str] | None = None,
) -> dict:
    """Get PR status and diff for a task (any authenticated caller).

    Returns the ``pr_service.get_pr_status`` dict as-is. The ``deep`` KG context
    is a per-surface adapter concern (DECISION 2) — the adapter attaches it.
    Raises :class:`NotFoundError` (404) when no PR exists for the task.
    """
    workspace_id = require_workspace_ctx(ctx)
    if visible_projects is not None:
        task_cursor = await db.execute(
            "SELECT project FROM tasks WHERE id = ? AND workspace_id = ? "
            "AND deleted_at IS NULL",
            (task_id, workspace_id),
        )
        task_row = await task_cursor.fetchone()
        if task_row is None or (
            task_row["project"] is not None
            and task_row["project"] not in visible_projects
        ):
            raise NotFoundError(code="pr_not_found", message="Pull request not found")
    from core.api.services import pr_service

    try:
        return await pr_service.get_pr_status(
            task_id, db, workspace_id=workspace_id
        )
    except ValueError as exc:
        raise NotFoundError(code="pr_not_found", message=str(exc))


# ---------------------------------------------------------------------------
# Use cases — writes (branch/PR lifecycle)
# ---------------------------------------------------------------------------


async def create_branch(
    ctx: CallerContext,
    db: aiosqlite.Connection,
    *,
    task_id: str,
) -> dict:
    """Create git worktree and branch for a task (operator+). Idempotent.

    Raises :class:`ConflictError` (409) on a ``ValueError`` (matching the legacy
    409) and re-raises :class:`GitOpsError` (-> 500 in the adapter).
    """
    require_role_ctx(ctx, "operator", "admin", "super_admin")
    workspace_id = require_workspace_ctx(ctx)

    from core.api.services import pr_service

    try:
        return await pr_service.start_branch_short_write(
            task_id, db, workspace_id=workspace_id
        )
    except ValueError as exc:
        raise ConflictError(code="branch_conflict", message=str(exc))
    # GitOpsError propagates; the adapter maps it to the legacy 500.


async def register_branch(
    ctx: CallerContext,
    db: aiosqlite.Connection,
    *,
    task_id: str,
    branch_name: str,
    worktree_path: str | None = None,
) -> dict:
    """Register an externally-created branch as a draft PR record (operator+).

    Idempotent: returns the existing active PR if one already exists. Raises
    :class:`ValidationError` (422) on a ``ValueError`` (matching the legacy 422).
    """
    require_role_ctx(ctx, "operator", "admin", "super_admin")
    workspace_id = require_workspace_ctx(ctx)

    from core.api.services import pr_service

    try:
        return await pr_service.register_branch(
            task_id,
            branch_name,
            db,
            worktree_path=worktree_path,
            workspace_id=workspace_id,
        )
    except ValueError as exc:
        raise ValidationError(code="register_branch_failed", message=str(exc))


async def submit_pull_request(
    ctx: CallerContext,
    db: aiosqlite.Connection,
    *,
    task_id: str,
    title: str,
    body: str = "",
) -> dict:
    """Submit branch for review — moves the PR from draft to open (operator+).

    ``submitted_by`` is ``ctx.user_id`` (the original router passed
    ``user.user_id``). Raises :class:`ValidationError` (422) on a ``ValueError``
    and re-raises :class:`GitOpsError` (-> 500 in the adapter).
    """
    require_role_ctx(ctx, "operator", "admin", "super_admin")
    workspace_id = require_workspace_ctx(ctx)

    from core.api.services import pr_service

    try:
        return await pr_service.submit_pr(
            task_id,
            title,
            body,
            db,
            submitted_by=ctx.user_id,
            workspace_id=workspace_id,
        )
    except ValueError as exc:
        raise ValidationError(code="submit_pr_failed", message=str(exc))
    # GitOpsError propagates; the adapter maps it to the legacy 500.


async def merge_pull_request(
    ctx: CallerContext,
    db: aiosqlite.Connection,
    *,
    task_id: str,
) -> dict:
    """Merge a PR with operator+ and persisted human/delegated authority.

    Writes a ``pr.merge`` audit entry (``merger_id=ctx.user_id``) on success.
    Raises :class:`ValidationError` (422) on a ``ValueError`` and re-raises both
    :class:`MergeConflictError` (-> legacy 409 dict body) and plain
    :class:`GitOpsError` (-> legacy 500 plain body) UNCHANGED so the adapter can
    preserve those exact legacy bodies.
    """
    require_role_ctx(ctx, "operator", "admin", "super_admin")
    workspace_id = require_workspace_ctx(ctx)
    authority = await _require_pr_lifecycle_authority(ctx, db)

    from core.api.services import pr_service
    correlation_id = uuid.uuid4().hex

    await _append_pr_lifecycle_audit(
        ctx,
        db,
        operation="merge",
        stage="intent",
        task_id=task_id,
        correlation_id=correlation_id,
        authority=authority,
    )

    try:
        result = await pr_service.merge_pr(
            task_id,
            db,
            merger_id=ctx.user_id,
            workspace_id=workspace_id,
        )
    except Exception as exc:
        if db.in_transaction:
            await db.rollback()
        await _append_pr_lifecycle_audit(
            ctx,
            db,
            operation="merge",
            stage="failed",
            task_id=task_id,
            correlation_id=correlation_id,
            authority=authority,
            failure_type=type(exc).__name__,
        )
        if isinstance(exc, ValueError):
            raise ValidationError(code="merge_failed", message=str(exc)) from exc
        raise
    # MergeConflictError (subclass of GitOpsError) and plain GitOpsError propagate;
    # the adapter maps them to the exact legacy 409 dict / 500 plain-string bodies.

    await _append_pr_lifecycle_audit(
        ctx,
        db,
        operation="merge",
        stage="confirmed",
        task_id=task_id,
        correlation_id=correlation_id,
        authority=authority,
    )
    return result


async def approve_pull_request(
    ctx: CallerContext,
    db: aiosqlite.Connection,
    *,
    task_id: str,
    reviewer,
) -> dict:
    """Approve a PR (operator+, team_admin of the project or admin+). Four-eyes:
    cannot approve your own PR.

    ``pr_service.approve_pr`` needs the full ``UserInfo`` (``user_id`` + team
    lookups), which is richer than ``CallerContext``, so the adapter passes the
    resolved ``reviewer`` (the ``UserInfo``) through. Raises
    :class:`ValidationError` (422) on a ``ValueError`` and :class:`AuthorizationError`
    (403) on a ``PermissionError`` — matching the legacy router.
    """
    require_role_ctx(ctx, "operator", "admin", "super_admin")
    workspace_id = require_workspace_ctx(ctx)

    from core.api.services import pr_service
    from core.api.use_cases._errors import AuthorizationError

    try:
        return await pr_service.approve_pr(
            task_id, reviewer, db, workspace_id=workspace_id
        )
    except ValueError as exc:
        raise ValidationError(code="approve_pr_failed", message=str(exc))
    except PermissionError as exc:
        raise AuthorizationError(code="approve_pr_forbidden", message=str(exc))


async def request_pull_request_changes(
    ctx: CallerContext,
    db: aiosqlite.Connection,
    *,
    task_id: str,
    reviewer,
    comment: str,
) -> dict:
    """Request changes on a PR (operator+). Revokes approval, sends the task back
    to in_progress.

    Like :func:`approve_pull_request`, the adapter passes the full ``UserInfo``
    ``reviewer``. Raises :class:`ValidationError` (422) on a ``ValueError`` and
    :class:`AuthorizationError` (403) on a ``PermissionError``.
    """
    require_role_ctx(ctx, "operator", "admin", "super_admin")
    workspace_id = require_workspace_ctx(ctx)

    from core.api.services import pr_service
    from core.api.use_cases._errors import AuthorizationError

    try:
        return await pr_service.request_changes_pr(
            task_id,
            reviewer,
            comment,
            db,
            workspace_id=workspace_id,
        )
    except ValueError as exc:
        raise ValidationError(code="request_changes_failed", message=str(exc))
    except PermissionError as exc:
        raise AuthorizationError(code="request_changes_forbidden", message=str(exc))


async def close_pull_request(
    ctx: CallerContext,
    db: aiosqlite.Connection,
    *,
    task_id: str,
    reason: str = "",
) -> dict:
    """Close a PR without merge — the task returns to in_progress (operator+).

    Raises :class:`ValidationError` (422) on a ``ValueError`` (matching legacy).
    """
    require_role_ctx(ctx, "operator", "admin", "super_admin")
    workspace_id = require_workspace_ctx(ctx)

    from core.api.services import pr_service

    try:
        return await pr_service.close_pr(
            task_id, reason, db, workspace_id=workspace_id
        )
    except ValueError as exc:
        raise ValidationError(code="close_pr_failed", message=str(exc))


async def update_pull_request(
    ctx: CallerContext,
    db: aiosqlite.Connection,
    *,
    task_id: str,
    title: str | None = None,
    body: str | None = None,
) -> dict:
    """Update a PR title or body (operator+).

    Raises :class:`ValidationError` (422) on a ``ValueError`` (matching legacy).
    """
    require_role_ctx(ctx, "operator", "admin", "super_admin")
    workspace_id = require_workspace_ctx(ctx)

    from core.api.services import pr_service

    try:
        return await pr_service.update_pr(
            task_id,
            db,
            title=title,
            body=body,
            workspace_id=workspace_id,
        )
    except ValueError as exc:
        raise ValidationError(code="update_pr_failed", message=str(exc))


async def revert_pull_request(
    ctx: CallerContext,
    db: aiosqlite.Connection,
    *,
    task_id: str,
) -> dict:
    """Revert a merged PR with operator+ and persisted human/delegated authority.

    Writes a ``pr.revert`` audit entry on success. Raises :class:`ValidationError`
    (422) on a ``ValueError`` and re-raises :class:`GitOpsError` (-> 500).
    """
    require_role_ctx(ctx, "operator", "admin", "super_admin")
    workspace_id = require_workspace_ctx(ctx)
    authority = await _require_pr_lifecycle_authority(ctx, db)

    from core.api.services import pr_service
    correlation_id = uuid.uuid4().hex

    await _append_pr_lifecycle_audit(
        ctx,
        db,
        operation="revert",
        stage="intent",
        task_id=task_id,
        correlation_id=correlation_id,
        authority=authority,
    )

    try:
        result = await pr_service.revert_pr(
            task_id, db, workspace_id=workspace_id
        )
    except Exception as exc:
        if db.in_transaction:
            await db.rollback()
        await _append_pr_lifecycle_audit(
            ctx,
            db,
            operation="revert",
            stage="failed",
            task_id=task_id,
            correlation_id=correlation_id,
            authority=authority,
            failure_type=type(exc).__name__,
        )
        if isinstance(exc, ValueError):
            raise ValidationError(code="revert_pr_failed", message=str(exc)) from exc
        raise
    # GitOpsError propagates; the adapter maps it to the legacy 500.

    await _append_pr_lifecycle_audit(
        ctx,
        db,
        operation="revert",
        stage="confirmed",
        task_id=task_id,
        correlation_id=correlation_id,
        authority=authority,
    )
    return result


__all__ = [
    "GitOpsError",
    "MergeConflictError",
    "PR_LIFECYCLE_REQUIRES_HUMAN_DETAIL",
    "ServiceError",
    "get_merge_conflicts",
    "get_pull_request",
    "create_branch",
    "register_branch",
    "submit_pull_request",
    "merge_pull_request",
    "approve_pull_request",
    "request_pull_request_changes",
    "close_pull_request",
    "update_pull_request",
    "revert_pull_request",
]
