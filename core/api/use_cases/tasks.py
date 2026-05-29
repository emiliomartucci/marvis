# v1.0.0 - 2026-05-27 - S1 F1.6: tasks use_cases extracted from router (CORNERSTONE — human-approval gate)
"""Tasks use_cases — pure domain logic, transport-agnostic (no ``fastapi``).

Sibling of :mod:`core.api.use_cases.learnings` (the S1 "collapse runtime"
TEMPLATE). One pure async function per operation, signature
``(ctx: CallerContext, db, *typed_args) -> <DTO>``. The HTTP router becomes a thin
adapter that resolves identity into a :class:`CallerContext`, calls these
functions, and maps :class:`ServiceError` -> ``HTTPException`` via
``routers/_adapter.to_http``. The Python MCP surface (later) calls the SAME
functions with ``CallerContext.local_single_user()``. One implementation, no fork.

This is the **cornerstone** router: it owns the human-approval gate (the
four-eyes check on ``pending -> approved``). Notes on how the template lands:

CORNERSTONE — the human-approval gate moves to ``ctx.is_human_session``.
    The legacy guard was ``if not request.cookies.get("pir_session"): raise 403``.
    That cookie check is a transport detail; the DOMAIN rule is "only a human
    session may approve". So :func:`update_task` checks ``ctx.is_human_session``
    and raises :class:`AuthorizationError(code="approval_requires_human")`. The
    HTTP adapter fills ``is_human_session`` from the cookie
    (``CallerContext.from_user_info(user, is_human_session=bool(cookie))``); the
    MCP/local surface uses ``local_single_user()`` which sets it ``True`` (the
    four-eyes collapses locally, by design). The HTTP adapter MUST preserve the
    exact legacy PLAIN-STRING 403 body (pinned by
    ``tests/test_tasks.py::test_pending_to_approved_requires_cookie_auth``, which
    asserts ``"human authorization" in detail.lower()``), so it catches this one
    ``AuthorizationError`` and re-raises the legacy ``HTTPException(403, <string>)``
    itself — the audit-router pattern. :data:`APPROVAL_REQUIRES_HUMAN_DETAIL`
    holds the exact string so router and use_case never drift.

DECISION 1 — Visibility resolution at the adapter, enforcement in the use_case.
    ``list_tasks`` and ``get_task`` filter by ``get_visible_projects`` (needs
    ``UserInfo.teams``, NOT carried by ``CallerContext``). The ADAPTER resolves
    ``visible_projects`` and passes it in; the use_case enforces it.
    IMPORTANT — the two endpoints differ: ``get_task`` RAISES 404 when the
    project is not visible (does not reveal existence). ``list_tasks`` does NOT
    raise — it returns ``[]`` when a requested project is not visible, or filters
    the query to the visible set (NULL project excluded). Both replicated exactly.
    ``visible_projects=None`` means "no restriction" (admin/agent or MCP/local).

DECISION 2 — ``deep`` KG enrichment on ``get_task`` is a per-surface adapter
    concern (rate-limit + access log + ``build_kg_context_for_task``). The
    use_case returns the task with ``kg_context=None``; the adapter attaches it.

DECISION 3 — the ``list_tasks`` ``deep_requires_filter`` 400 stays in the adapter
    (transport input guard tied to the adapter-owned ``deep`` feature).

CALLABLE SEAMS (the costs ``programs_loader`` pattern) — three behaviors live in
the router module so existing test seams keep working and ``use_cases`` stays
fastapi-free:
  * ``sync_graph`` — ``create_task`` receives ``sync_task_to_graph`` as a kwarg.
    The router's ``create_task`` source literally references the symbol (pinned
    by ``test_task_sync_to_graph.py::test_router_calls_sync_only_in_create_handler``)
    and ``update_task`` never does.
  * ``schedule_embed`` — ``create_task``/``update_task`` receive
    ``_schedule_embed_task`` (defined in the router and re-exported there for
    ``tests/test_task_auto_embedding.py`` which calls ``tasks_router._schedule_embed_task``).
  * ``requires_pr_gate`` — ``update_task`` receives a callable resolved from the
    router's ``_project_requires_pr_gate`` at call time, so
    ``monkeypatch.setattr(tasks_router, "_project_requires_pr_gate", ...)`` (used
    by the completed-PR-gate tests) takes effect.

The response/request DTOs (``TaskDetailResponse`` / ``TaskListResponse`` /
``TaskSummary`` / ``TaskCreateRequest`` / ``TaskUpdateRequest`` /
``TaskCostSummary`` / ``HumanCostEntryCreate`` / ``VALID_TRANSITIONS`` / etc.)
live in ``core.api.models`` and are NOT moved (like costs). ``ZombieScanResponse``
/ ``BulkRejectResponse`` (router-local response DTOs) and ``BulkRejectRequest``
move here; the router re-exports them for ``response_model=``.
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable

import aiosqlite
from pydantic import BaseModel, Field

from core.api.models import (
    VALID_TRANSITIONS,
    CommentReaction,
    CommentResponse,
    ProjectStatusBreakdown,
    ProjectSummary,
    StatusCounts,
    TaskCostEntry,
    TaskCostSummary,
    TaskDetailResponse,
    TaskListResponse,
    TaskSummary,
    UserSummary,
)
from core.api.use_cases._context import CallerContext, require_role_ctx
from core.api.use_cases._errors import (
    AuthorizationError,
    ConflictError,
    NotFoundError,
    ValidationError,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Domain constants + legacy error/body parity strings
# ---------------------------------------------------------------------------

ZOMBIE_THRESHOLD_DAYS_DEFAULT = 21

# Exact legacy 403 body for the human-approval gate. The HTTP adapter re-raises
# this verbatim (preserving the plain-string detail) for the
# code="approval_requires_human" AuthorizationError. Keep router + use_case in sync.
APPROVAL_REQUIRES_HUMAN_DETAIL = (
    "Task approval (pending→approved) requires human authorization via Console Triage. "
    "Use a valid pir_session cookie; Bearer-only agent requests cannot approve tasks. "
    "Agents cannot approve tasks. Tasks are auto-approved at creation via ICE-D policy. "
    "If still pending, wait for human approval in Console. "
    "Agents can set: in_progress, completed, failed, rejected."
)

_TASK_NOT_FOUND_MESSAGE = (
    "Task not found (task_id={task_id!r}). "
    "Reason: no row in tasks with this id, visible to your workspace, and not soft-deleted. "
    "Fix: verify the task_id with mcp__pir__list_tasks or mcp__pir__search. "
    "If you're an agent, check X-Agent-Name maps to a workspace that owns this task."
)

_PR_STATUS_SUBQUERY = """(
    SELECT pr.status FROM pull_requests pr
    WHERE pr.task_id = tasks.id
    ORDER BY CASE pr.status
        WHEN 'merging' THEN 1 WHEN 'open' THEN 2 WHEN 'draft' THEN 3
        WHEN 'merged' THEN 4 WHEN 'closed' THEN 5
    END LIMIT 1
) AS pr_status"""


# ---------------------------------------------------------------------------
# Router-local response/request DTOs (moved from the router; re-exported there)
# ---------------------------------------------------------------------------


class ZombieScanResponse(BaseModel):
    threshold_days: int
    dry_run: bool
    total_zombies: int
    by_project: dict[str, list[dict]]
    notifications_created: int


class BulkRejectRequest(BaseModel):
    task_ids: list[str] = Field(..., min_length=1, max_length=500)
    reason: str = Field("aging_zombie", max_length=100)


class BulkRejectResponse(BaseModel):
    rejected: list[str]
    failed: list[dict]
    total: int


# ---------------------------------------------------------------------------
# Pure helpers (moved verbatim from the router)
# ---------------------------------------------------------------------------


def _task_not_found(task_id: str) -> NotFoundError:
    return NotFoundError(
        code="task_not_found",
        message=_TASK_NOT_FOUND_MESSAGE.format(task_id=task_id),
    )


def _base_task_fields(row: aiosqlite.Row) -> dict:
    """Extract common lightweight fields from a SQLite row."""
    tags_raw = row["tags"] or "[]"
    try:
        tags = json.loads(tags_raw)
    except (json.JSONDecodeError, TypeError):
        tags = []
    return dict(
        id=row["id"],
        title=row["title"],
        kind=row["kind"] if "kind" in row.keys() else "normal",
        status=row["status"],
        project=row["project"],
        priority=row["priority"],
        created_by=row["created_by"],
        owner_id=row["owner_id"],
        source=row["source"],
        source_ref=row["source_ref"],
        tags=tags,
        deleted_at=row["deleted_at"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        impact=row["impact"],
        confidence=row["confidence"],
        ease=row["ease"],
        delegation=row["delegation"],
        ice_score=row["ice_score"],
        scored_by=row["scored_by"],
        scored_at=row["scored_at"],
        due_date=row["due_date"] if "due_date" in row.keys() else None,
        reminder_sent_at=row["reminder_sent_at"]
        if "reminder_sent_at" in row.keys()
        else None,
        completion_mode=(
            row["completion_mode"]
            if "completion_mode" in row.keys() and row["completion_mode"]
            else "pr"
        ),
    )


def _row_to_task_list(row: aiosqlite.Row) -> TaskListResponse:
    """Convert SQLite row to TaskListResponse (lightweight, no embedded objects)."""
    try:
        pr_status = row["pr_status"]
    except (IndexError, KeyError):
        pr_status = None
    return TaskListResponse(**_base_task_fields(row), pr_status=pr_status)


def _row_to_task(
    row: aiosqlite.Row, owner_row: aiosqlite.Row | None = None
) -> TaskDetailResponse:
    """Convert SQLite row to TaskDetailResponse.

    owner_row: optional Row from users table for embedded owner summary.
    """
    # pr_status comes from subquery, not present in all queries
    try:
        pr_status = row["pr_status"]
    except (IndexError, KeyError):
        pr_status = None
    # review_feedback added in migration 019
    try:
        review_feedback = row["review_feedback"]
    except (IndexError, KeyError):
        review_feedback = None

    owner: UserSummary | None = None
    if owner_row:
        owner = UserSummary(
            id=owner_row["id"],
            slug=owner_row["slug"],
            display_name=owner_row["display_name"],
            avatar_color=owner_row["avatar_color"] or "#6366f1",
        )

    return TaskDetailResponse(
        **_base_task_fields(row),
        description=row["description"],
        owner=owner,
        pr_status=pr_status,
        review_feedback=review_feedback,
    )


# ---------------------------------------------------------------------------
# Use cases — reads
# ---------------------------------------------------------------------------


async def get_tasks_summary(
    ctx: CallerContext,
    db: aiosqlite.Connection,
) -> TaskSummary:
    """Cross-project task summary (any authenticated caller)."""
    ws = ctx.workspace_id or "ws_default"

    # Total active tasks
    cursor = await db.execute(
        "SELECT COUNT(*) FROM tasks WHERE deleted_at IS NULL AND COALESCE(workspace_id, 'ws_default') = ?",
        (ws,),
    )
    total = (await cursor.fetchone())[0]

    # By status
    cursor = await db.execute(
        "SELECT status, COUNT(*) as cnt FROM tasks WHERE deleted_at IS NULL AND COALESCE(workspace_id, 'ws_default') = ? GROUP BY status",
        (ws,),
    )
    status_dict: dict[str, int] = {}
    async for row in cursor:
        status_dict[row["status"]] = row["cnt"]
    by_status = StatusCounts(**status_dict)

    # By project (only open statuses)
    cursor = await db.execute(
        "SELECT project, status, COUNT(*) as cnt FROM tasks "
        "WHERE deleted_at IS NULL AND status IN ('pending', 'approved', 'in_progress', 'review') "
        "AND COALESCE(workspace_id, 'ws_default') = ? "
        "GROUP BY project, status",
        (ws,),
    )
    project_map: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    async for row in cursor:
        project_map[row["project"]][row["status"]] = row["cnt"]
    by_project = [
        ProjectStatusBreakdown(project=p, **counts) for p, counts in project_map.items()
    ]

    # By priority
    cursor = await db.execute(
        "SELECT priority, COUNT(*) as cnt FROM tasks WHERE deleted_at IS NULL GROUP BY priority"
    )
    by_priority: dict[str, int] = {}
    async for row in cursor:
        by_priority[row["priority"]] = row["cnt"]

    return TaskSummary(
        total=total, by_status=by_status, by_project=by_project, by_priority=by_priority
    )


async def get_task_projects(
    ctx: CallerContext,
    db: aiosqlite.Connection,
) -> list[ProjectSummary]:
    """List projects with task counts (any authenticated caller)."""
    cursor = await db.execute(
        "SELECT project, "
        "SUM(CASE WHEN status IN ('pending', 'approved', 'in_progress', 'review') THEN 1 ELSE 0 END) as open_count, "
        "COUNT(*) as total_count "
        "FROM tasks WHERE deleted_at IS NULL GROUP BY project ORDER BY project"
    )
    return [
        ProjectSummary(
            project=row["project"],
            open_count=row["open_count"],
            total_count=row["total_count"],
        )
        async for row in cursor
    ]


async def list_tasks(
    ctx: CallerContext,
    db: aiosqlite.Connection,
    *,
    project: str | None = None,
    status: str | None = None,
    kind: str | None = None,
    priority: str | None = None,
    created_by: str | None = None,
    owner_id: str | None = None,
    delegation: str | None = None,
    tags: str | None = None,
    limit: int = 20,
    offset: int = 0,
    sort: str = "created_at:desc",
    include_deleted: bool = False,
    detailed: bool = False,
    visible_projects: set[str] | None = None,
) -> list[TaskDetailResponse]:
    """List tasks with filters (any authenticated caller).

    Visibility (DECISION 1): the adapter resolves ``visible_projects``; this
    use_case enforces it WITHOUT raising — it returns ``[]`` when a requested
    project is not visible, or restricts the query to the visible set (NULL
    project excluded). ``None`` means unrestricted (admin/agent or MCP/local).
    """
    conditions: list[str] = []
    params: list[str] = []

    # Workspace isolation: always scope to caller's workspace
    ws = ctx.workspace_id or "ws_default"
    conditions.append("COALESCE(workspace_id, 'ws_default') = ?")
    params.append(ws)

    if not include_deleted:
        conditions.append("deleted_at IS NULL")

    if project:
        conditions.append("project = ?")
        params.append(project)

    if status:
        statuses = [s.strip() for s in status.split(",")]
        placeholders = ",".join("?" for _ in statuses)
        conditions.append(f"status IN ({placeholders})")
        params.extend(statuses)

    if kind:
        conditions.append("kind = ?")
        params.append(kind)

    if priority:
        conditions.append("priority = ?")
        params.append(priority)

    if created_by:
        conditions.append("created_by = ?")
        params.append(created_by)

    if owner_id:
        conditions.append("owner_id = ?")
        params.append(owner_id)

    if delegation:
        if delegation == "unscored":
            conditions.append("delegation IS NULL")
        else:
            conditions.append("delegation = ?")
            params.append(delegation)

    if tags:
        tag_list = [t.strip() for t in tags.split(",")]
        tag_conditions = []
        for tag in tag_list:
            tag_conditions.append("tags LIKE ?")
            params.append(f'%"{tag}"%')
        conditions.append(f"({' OR '.join(tag_conditions)})")

    # Project visibility enforcement: operators/viewers only see tasks in visible projects
    if visible_projects is not None:
        if project:
            # Specific project requested — deny if not in visible set (return [], not raise)
            if project not in visible_projects:
                return []
        else:
            # No project filter — restrict to visible projects (NULL project excluded)
            if not visible_projects:
                return []
            placeholders = ",".join("?" for _ in visible_projects)
            conditions.append(f"project IN ({placeholders})")
            params.extend(list(visible_projects))

    where = " AND ".join(conditions) if conditions else "1=1"

    # Parse sort
    valid_sort_fields = {
        "created_at",
        "updated_at",
        "priority",
        "status",
        "project",
        "ice_score",
        "delegation",
    }
    sort_field, _, sort_dir = sort.partition(":")
    if sort_field not in valid_sort_fields:
        sort_field = "created_at"
    sort_dir = "ASC" if sort_dir.lower() == "asc" else "DESC"

    # NULLS LAST for ice_score sorting (unscored tasks at bottom)
    nulls_clause = " NULLS LAST" if sort_field == "ice_score" else ""
    query = f"SELECT *, {_PR_STATUS_SUBQUERY} FROM tasks WHERE {where} ORDER BY {sort_field} {sort_dir}{nulls_clause} LIMIT ? OFFSET ?"
    params.extend([str(limit), str(offset)])

    cursor = await db.execute(query, params)

    if not detailed:
        return [_row_to_task_list(row) async for row in cursor]

    # detailed=True: include description + comments
    tasks = [_row_to_task(row) async for row in cursor]
    if not tasks:
        return tasks

    # Batch fetch comments for all tasks in one query
    task_ids = [t.id for t in tasks]
    placeholders = ",".join("?" for _ in task_ids)
    comments_cursor = await db.execute(
        f"SELECT c.*, GROUP_CONCAT(cr.reaction || ':' || cr.created_by, '|') as reactions_raw "
        f"FROM comments c LEFT JOIN comment_reactions cr ON cr.comment_id = c.id "
        f"WHERE c.target_type = 'task' AND c.target_id IN ({placeholders}) AND c.deleted_at IS NULL "
        f"GROUP BY c.id ORDER BY c.created_at ASC",
        task_ids,
    )
    # Group comments by task_id
    comments_by_task: dict[str, list] = defaultdict(list)
    async for crow in comments_cursor:
        reactions = []
        if crow["reactions_raw"]:
            for part in crow["reactions_raw"].split("|"):
                pieces = part.split(":", 1)
                if len(pieces) == 2:
                    reactions.append(
                        CommentReaction(reaction=pieces[0], created_by=pieces[1])
                    )
        comment = CommentResponse(
            id=crow["id"],
            target_type=crow["target_type"],
            target_id=crow["target_id"],
            body=crow["body"],
            status=crow["status"],
            created_by=crow["created_by"],
            created_at=crow["created_at"],
            edited_at=crow["edited_at"],
            parent_id=crow["parent_id"],
            reactions=reactions,
            replies=[],
        )
        comments_by_task[crow["target_id"]].append(comment)

    for task in tasks:
        task.comments = comments_by_task.get(task.id, [])

    return tasks


async def get_task(
    ctx: CallerContext,
    db: aiosqlite.Connection,
    *,
    task_id: str,
    visible_projects: set[str] | None = None,
) -> TaskDetailResponse:
    """Get a single task by ID (any authenticated caller).

    Visibility (DECISION 1): the adapter resolves ``visible_projects``; this
    use_case RAISES :class:`NotFoundError` (404, identical body) when the task's
    project is not visible — does not reveal existence. ``None`` is unrestricted.

    Returns ``kg_context=None``; the adapter attaches it when ``deep`` (DECISION 2).
    """
    ws = ctx.workspace_id or "ws_default"
    cursor = await db.execute(
        f"SELECT *, {_PR_STATUS_SUBQUERY} FROM tasks WHERE id = ? AND deleted_at IS NULL AND COALESCE(workspace_id, 'ws_default') = ?",
        (task_id, ws),
    )
    row = await cursor.fetchone()
    if not row:
        raise _task_not_found(task_id)

    # Visibility check: operators cannot see tasks from projects outside their team
    if row["project"]:
        if visible_projects is not None and row["project"] not in visible_projects:
            raise _task_not_found(task_id)

    return _row_to_task(row)


async def get_task_cost_entries(
    ctx: CallerContext,
    db: aiosqlite.Connection,
    *,
    task_id: str,
) -> TaskCostSummary:
    """Return cost summary + all entries for a task (any authenticated caller)."""
    from core.api.services import cost_service

    ws = ctx.workspace_id or "ws_default"
    cursor = await db.execute(
        "SELECT id FROM tasks WHERE id = ? AND deleted_at IS NULL AND COALESCE(workspace_id, 'ws_default') = ?",
        (task_id, ws),
    )
    if not await cursor.fetchone():
        raise _task_not_found(task_id)

    summary = await cost_service.get_task_cost_summary(db, task_id)

    cursor_entries = await db.execute(
        """SELECT id, task_id, entry_type, source, conversation_id, pr_id,
                  cost_usd_delta, agent_seconds, human_minutes,
                  total_cost_usd, total_bill_usd, is_billable,
                  billable_reason, description, created_by, created_at
           FROM task_cost_entries
           WHERE task_id = ?
           ORDER BY created_at DESC""",
        (task_id,),
    )
    rows = await cursor_entries.fetchall()
    entries = [
        TaskCostEntry(
            **{k: row[k] for k in row.keys()},
            is_billable=bool(row["is_billable"]),
        )
        for row in rows
    ]

    return TaskCostSummary(**summary, entries=entries)


# ---------------------------------------------------------------------------
# Use cases — writes
# ---------------------------------------------------------------------------


async def create_task(
    ctx: CallerContext,
    db: aiosqlite.Connection,
    *,
    body,
    created_by: str,
    sync_graph: Callable[..., Awaitable[bool]],
    schedule_embed: Callable[..., None],
) -> TaskDetailResponse:
    """Create a new task (operator+).

    ``created_by`` is resolved by the adapter (session-owner attribution from the
    ``X-Session-Name`` header, falling back to the caller username) — a transport
    concern that must not reach the use_case via ``request``.

    ``sync_graph`` / ``schedule_embed`` are passed in (the costs ``programs_loader``
    seam): ``sync_graph`` is the router's ``sync_task_to_graph`` reference and
    ``schedule_embed`` the router's ``_schedule_embed_task`` — both kept callable
    so existing import/monkeypatch test seams stay valid and this module stays
    fastapi-free.
    """
    require_role_ctx(ctx, "operator", "admin", "super_admin")

    from core.api.services.audit import log_audit
    from core.api.services.auto_approval import DEFAULT_POLICY, ApprovalDecision
    from core.api.services.events import emit_event

    task_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    tags_json = json.dumps(body.tags)

    created_by_value = created_by

    # Scoring attribution
    scored_by = (
        created_by_value
        if any([body.impact, body.confidence, body.ease, body.delegation])
        else None
    )
    scored_at = now if scored_by else None

    # Default owner: use provided owner_id; if missing, lookup Responsible RACI of the project
    effective_owner_id = body.owner_id
    if not effective_owner_id and body.project:
        db.row_factory = aiosqlite.Row
        raci_row = await (
            await db.execute(
                "SELECT user_id FROM project_raci WHERE project = ? AND role = 'responsible'",
                (body.project,),
            )
        ).fetchone()
        if raci_row:
            effective_owner_id = raci_row["user_id"]

    # Auto-approval: evaluate policy BEFORE insert (atomic — no two-step update)
    decision, approval_reason = DEFAULT_POLICY.evaluate(
        delegation=body.delegation,
        ease=body.ease,
        impact=body.impact,
        confidence=body.confidence,
        scored_by=scored_by,
        created_by=created_by_value,
    )
    initial_status = (
        "approved" if decision == ApprovalDecision.AUTO_APPROVED else "pending"
    )

    try:
        await db.execute(
            "INSERT INTO tasks (id, title, description, status, project, priority, "
            "created_by, owner_id, source, source_ref, tags, kind, "
            "impact, confidence, ease, delegation, scored_by, scored_at, "
            "due_date, workspace_id, completion_mode, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                task_id,
                body.title,
                body.description,
                initial_status,
                body.project,
                body.priority,
                created_by_value,
                effective_owner_id,
                body.source,
                body.source_ref,
                tags_json,
                body.kind,
                body.impact,
                body.confidence,
                body.ease,
                body.delegation,
                scored_by,
                scored_at,
                body.due_date,
                ctx.workspace_id or "ws_default",
                body.completion_mode,
                now,
                now,
            ),
        )
        await db.commit()
    except aiosqlite.IntegrityError:
        raise ConflictError(
            code="duplicate_task",
            message=(
                "Duplicate task: a task with this (source, source_ref) pair already exists. "
                "Reason: idempotency guard — same source+source_ref should map to a single task to prevent doubles from retries. "
                "Fix: either (a) fetch the existing task with mcp__pir__list_tasks and continue with it, "
                "(b) pass a unique source_ref if this is genuinely a new task, or "
                "(c) omit source_ref to let the server auto-generate one."
            ),
        )

    # KG sync: emit task:artifact:{uuid} node so handoffs created in the same
    # session can resolve describes edges without waiting for the nightly
    # populate_artifacts batch. Non-blocking — failures are logged and
    # swallowed so a KG hiccup never turns a successful create into a
    # 5xx. populate_tasks_and_prs remains idempotent over this node (UPSERT).
    await sync_graph(
        db,
        task_id=task_id,
        title=body.title,
        project=body.project,
        priority=body.priority,
        source=body.source,
        status=initial_status,
        created_at=now,
        updated_at=now,
    )

    # Audit log for auto-approved tasks (distinguishable from human approvals)
    if decision == ApprovalDecision.AUTO_APPROVED:
        await log_audit(
            db,
            action="task.auto_approved",
            user="system:auto_policy",
            resource_type="task",
            resource_id=task_id,
            details={
                "policy_version": "v1",
                "approval_reason": approval_reason,
                "delegation": body.delegation,
                "ease": body.ease,
                "impact": body.impact,
                "confidence": body.confidence,
                "scored_by": scored_by,
                "created_by": created_by_value,
            },
        )

    # Emit event + generate notification for new task
    event_payload = {
        "status": initial_status,
        "kind": body.kind,
        "title": body.title,
        "created_by": created_by_value,
        "delegation": body.delegation if hasattr(body, "delegation") else None,
        "impact": body.impact if hasattr(body, "impact") else None,
        "confidence": body.confidence if hasattr(body, "confidence") else None,
        "ease": body.ease if hasattr(body, "ease") else None,
    }
    event_id = await emit_event(
        db,
        "task.created",
        project=body.project,
        actor_id=created_by_value,
        target_type="task",
        target_id=task_id,
        payload=event_payload,
    )
    if event_id:
        from core.api.services.notification_service import generate_from_event

        await generate_from_event(
            db,
            "task.created",
            event_id,
            body.project,
            created_by_value,
            "task",
            task_id,
            event_payload,
        )

    # Fetch created task + owner summary
    db.row_factory = aiosqlite.Row
    cursor = await db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,))
    row = await cursor.fetchone()
    owner_row = None
    if row["owner_id"]:
        owner_row = await (
            await db.execute(
                "SELECT id, slug, display_name, avatar_color FROM users WHERE id = ?",
                (row["owner_id"],),
            )
        ).fetchone()

    # Auto-embed in background (non-blocking)
    schedule_embed(
        task_id=task_id,
        title=body.title,
        project=body.project or "",
        status=initial_status,
        workspace_id=ctx.workspace_id or "ws_default",
    )

    return _row_to_task(row, owner_row)


async def update_task(
    ctx: CallerContext,
    db: aiosqlite.Connection,
    *,
    task_id: str,
    body,
    requires_pr_gate: Callable[[str | None], bool],
    schedule_embed: Callable[..., None],
) -> TaskDetailResponse:
    """Update a task; validates status transitions (operator+).

    CORNERSTONE: the ``pending -> approved`` four-eyes gate checks
    ``ctx.is_human_session`` (filled from the cookie by the HTTP adapter, ``True``
    on the MCP/local surface). Raises :class:`AuthorizationError` with
    ``code="approval_requires_human"`` and the exact legacy detail string so the
    adapter can re-raise the plain-string 403 verbatim.

    ``requires_pr_gate`` is resolved from the router's ``_project_requires_pr_gate``
    at call time (monkeypatch seam). ``schedule_embed`` is the router's
    ``_schedule_embed_task``. NOTE: this function never references
    ``sync_task_to_graph`` — status changes are the batch populator's job
    (pinned by test_task_sync_to_graph).
    """
    require_role_ctx(ctx, "operator", "admin", "super_admin")

    from core.api.config import settings
    from core.api.services import cost_service
    from core.api.services.audit import log_audit
    from core.api.services.events import emit_event

    ws = ctx.workspace_id or "ws_default"
    cursor = await db.execute(
        "SELECT * FROM tasks WHERE id = ? AND deleted_at IS NULL AND COALESCE(workspace_id, 'ws_default') = ?",
        (task_id, ws),
    )
    row = await cursor.fetchone()
    if not row:
        raise _task_not_found(task_id)

    # Validate status transition
    if body.status and body.status != row["status"]:
        allowed = VALID_TRANSITIONS.get(row["status"], set())
        if body.status not in allowed:
            raise ValidationError(
                code="invalid_transition",
                message=(
                    f"Invalid transition: {row['status']} -> {body.status}. "
                    f"Allowed: {', '.join(sorted(allowed)) if allowed else 'none'}"
                ),
            )
        # Guard: manual PATCH to status=review requires an active PR (anti-bypass)
        if body.status == "review":
            pr_cursor = await db.execute(
                "SELECT id FROM pull_requests"
                " WHERE task_id = ? AND status IN ('draft', 'open', 'merging') LIMIT 1",
                (task_id,),
            )
            if not await pr_cursor.fetchone():
                raise ValidationError(
                    code="review_requires_active_pr",
                    message=(
                        "Cannot set status=review: no active PR for this task. "
                        "Reason: the 'review' state means 'human reviewing a PR' — without a PR there's nothing to review. "
                        "Fix: create the PR first via mcp__pir__register_branch(task_id, branch_name) "
                        "followed by mcp__pir__submit_pr(task_id, title, body). "
                        "The status will auto-advance to 'review' on submit."
                    ),
                )
        # Guard: cannot mark completed while a PR is still open (prevents zombie PRs)
        if body.status == "completed":
            active_pr_cursor = await db.execute(
                "SELECT id FROM pull_requests"
                " WHERE task_id = ? AND status IN ('draft', 'open', 'merging') LIMIT 1",
                (task_id,),
            )
            if await active_pr_cursor.fetchone():
                raise ValidationError(
                    code="completed_requires_no_open_pr",
                    message=(
                        "Cannot set status=completed: PR for this task is still open (status in draft/open/merging). "
                        "Reason: completing a task with an open PR leaves zombie PRs in the queue. "
                        "Fix: close the PR first — either merge it via Triage (happy path) or "
                        "mcp__pir__close_pr(task_id, reason='abandoned') if the work is being dropped, "
                        "then retry the PATCH to status=completed."
                    ),
                )
            # The merged-PR gate for code/system projects applies ONLY to tasks
            # with completion_mode='pr' (default). Research/plan/verify tasks
            # (completion_mode 'doc' or 'none') bypass the gate — they don't
            # produce a PR as their deliverable and would otherwise be stuck.
            try:
                row_completion_mode = row["completion_mode"] or "pr"
            except (IndexError, KeyError):
                row_completion_mode = "pr"
            if row_completion_mode == "pr" and requires_pr_gate(row["project"]):
                merged_pr_cursor = await db.execute(
                    "SELECT id FROM pull_requests"
                    " WHERE task_id = ? AND status = 'merged' LIMIT 1",
                    (task_id,),
                )
                if not await merged_pr_cursor.fetchone():
                    raise ValidationError(
                        code="completed_requires_merged_pr",
                        message=(
                            "Cannot set status=completed: code/system tasks with "
                            "completion_mode='pr' must complete through a merged PR workflow. "
                            "Use completion_mode='doc' for research/plan tasks or 'none' for "
                            "verify/diagnose tasks that have no PR."
                        ),
                    )
        # CORNERSTONE: pending→approved requires a human session (four-eyes gate)
        if body.status == "approved" and row["status"] == "pending":
            if not ctx.is_human_session:
                raise AuthorizationError(
                    code="approval_requires_human",
                    message=APPROVAL_REQUIRES_HUMAN_DETAIL,
                )

    old_status = row["status"]

    # Build SET clause (use model_fields_set to distinguish "not sent" from "sent as null")
    provided = body.model_fields_set
    updates: dict[str, str | None] = {}
    if "title" in provided:
        updates["title"] = body.title
    if "description" in provided:
        updates["description"] = body.description
    if "status" in provided:
        updates["status"] = body.status
    if "kind" in provided:
        updates["kind"] = body.kind
    if "priority" in provided:
        updates["priority"] = body.priority
    if "owner_id" in provided:
        updates["owner_id"] = body.owner_id
    if "tags" in provided:
        updates["tags"] = json.dumps(body.tags) if body.tags is not None else None
    if "due_date" in provided:
        updates["due_date"] = body.due_date
    if "completion_mode" in provided:
        updates["completion_mode"] = body.completion_mode
    # ICE-D scoring fields
    scoring_changed = False
    for field in ("impact", "confidence", "ease", "delegation"):
        if field in provided:
            updates[field] = getattr(body, field)
            scoring_changed = True
    if scoring_changed:
        updates["scored_by"] = ctx.username
        updates["scored_at"] = datetime.now(timezone.utc).isoformat()

    if not updates:
        raise ValidationError(
            code="no_fields_to_update",
            message=(
                "No fields to update. "
                "Reason: the PATCH body contained zero settable fields (all were omitted or not in the update allow-list). "
                "Fix: include at least one of: title, description, status, kind, priority, owner_id, tags, due_date, completion_mode, "
                "impact, confidence, ease, delegation."
            ),
        )

    now = datetime.now(timezone.utc).isoformat()
    updates["updated_at"] = now

    set_clause = ", ".join(f"{k} = ?" for k in updates)
    values = list(updates.values()) + [task_id]

    await db.execute(f"UPDATE tasks SET {set_clause} WHERE id = ?", values)
    await db.commit()

    # Cost tracking hook: fire-and-forget on in_progress → completed
    if (
        "status" in provided
        and body.status == "completed"
        and old_status == "in_progress"
    ):
        cursor_conv = await db.execute(
            """SELECT sm.conversation_id, sm.working_seconds, t.project
               FROM tasks t
               LEFT JOIN sessions_meta sm ON sm.project_slug = t.project
               WHERE t.id = ?
               ORDER BY sm.last_active DESC LIMIT 1""",
            (task_id,),
        )
        row_conv = await cursor_conv.fetchone()
        if row_conv and row_conv["conversation_id"]:
            _cost_task = asyncio.create_task(
                cost_service.create_agent_entry(
                    task_id=task_id,
                    project_slug=row_conv["project"],
                    source="task_completed",
                    created_by=ctx.username,
                    db_path=settings.db_path,
                    conversation_id=row_conv["conversation_id"],
                    agent_seconds=row_conv["working_seconds"] or 0,
                )
            )

            def _log_cost_error(t: asyncio.Task) -> None:
                if not t.cancelled() and t.exception():
                    logger.warning(
                        "Cost entry task failed for task %s: %s", task_id, t.exception()
                    )

            _cost_task.add_done_callback(_log_cost_error)
        else:
            logger.info(
                "No active session found for cost entry on task %s completion", task_id
            )

    # Emit event on status change
    if "status" in provided and body.status and body.status != old_status:
        status_payload = {
            "old_status": old_status,
            "new_status": body.status,
            "title": row["title"],
        }
        event_id = await emit_event(
            db,
            "task.status_changed",
            project=row["project"],
            actor_id=ctx.user_id,
            target_type="task",
            target_id=task_id,
            payload=status_payload,
        )
        # Generate notification (only fires for completed status)
        if event_id:
            from core.api.services.notification_service import generate_from_event

            await generate_from_event(
                db,
                "task.status_changed",
                event_id,
                row["project"],
                ctx.user_id,
                "task",
                task_id,
                status_payload,
            )
        # Auto-mark task_pending notifications as acted/read when task leaves pending
        if old_status == "pending" and body.status in ("approved", "rejected"):
            now = datetime.now(timezone.utc).isoformat()
            await db.execute(
                """UPDATE notifications
                   SET acted_at = ?, read_at = COALESCE(read_at, ?)
                   WHERE target_id = ? AND target_type = 'task'
                   AND type = 'task_pending' AND acted_at IS NULL""",
                (now, now, task_id),
            )
        # Audit log for privileged status transitions
        if body.status in ("approved", "completed"):
            await log_audit(
                db,
                action=f"task.{body.status}",
                user=ctx.username,
                resource_type="task",
                resource_id=task_id,
                details={
                    "old_status": old_status,
                    "new_status": body.status,
                    "project": row["project"],
                },
            )

    db.row_factory = aiosqlite.Row
    cursor = await db.execute(
        f"SELECT *, {_PR_STATUS_SUBQUERY} FROM tasks WHERE id = ?", (task_id,)
    )
    row = await cursor.fetchone()
    owner_row = None
    if row["owner_id"]:
        owner_row = await (
            await db.execute(
                "SELECT id, slug, display_name, avatar_color FROM users WHERE id = ?",
                (row["owner_id"],),
            )
        ).fetchone()

    # Auto-embed in background (non-blocking)
    schedule_embed(
        task_id=task_id,
        title=row["title"],
        project=row["project"] or "",
        status=row["status"],
        workspace_id=row["workspace_id"] or "ws_default",
    )

    return _row_to_task(row, owner_row)


async def create_human_cost_entry(
    ctx: CallerContext,
    db: aiosqlite.Connection,
    *,
    task_id: str,
    body,
) -> TaskCostSummary:
    """Manually record human time for a task (cookie-auth caller; adapter-enforced)."""
    from core.api.services import cost_service

    ws = ctx.workspace_id or "ws_default"
    cursor = await db.execute(
        "SELECT project FROM tasks WHERE id = ? AND deleted_at IS NULL AND COALESCE(workspace_id, 'ws_default') = ?",
        (task_id, ws),
    )
    row = await cursor.fetchone()
    if not row:
        raise _task_not_found(task_id)

    result = await cost_service.create_human_entry(
        task_id=task_id,
        project_slug=row["project"],
        human_minutes=body.human_minutes,
        created_by=ctx.username,
        db=db,
        description=body.description,
        is_billable=body.is_billable,
        idempotency_key=body.idempotency_key,
    )
    await db.commit()

    summary = await cost_service.get_task_cost_summary(db, task_id)
    entry_id = result.get("entry_id") if not result.get("skipped") else None
    return TaskCostSummary(**summary, created_entry_id=entry_id)


async def delete_task(
    ctx: CallerContext,
    db: aiosqlite.Connection,
    *,
    task_id: str,
) -> None:
    """Soft delete a task (admin+). Cannot delete in_progress tasks."""
    require_role_ctx(ctx, "admin", "super_admin")

    from core.api.services.audit import log_audit

    ws = ctx.workspace_id or "ws_default"
    cursor = await db.execute(
        "SELECT * FROM tasks WHERE id = ? AND deleted_at IS NULL AND COALESCE(workspace_id, 'ws_default') = ?",
        (task_id, ws),
    )
    row = await cursor.fetchone()
    if not row:
        raise _task_not_found(task_id)

    if row["status"] == "in_progress":
        raise ConflictError(
            code="cannot_delete_in_progress",
            message=(
                "Cannot delete task in status='in_progress'. "
                "Reason: agents may still be writing to this task (commits, PR submission). "
                "Deleting it mid-flight would orphan the worktree and hide agent output from Triage. "
                "Fix: first PATCH status to 'completed'/'failed'/'rejected', or mcp__pir__close_pr "
                "if there's an associated PR, then delete."
            ),
        )

    now = datetime.now(timezone.utc).isoformat()
    await db.execute(
        "UPDATE tasks SET deleted_at = ?, updated_at = ? WHERE id = ?",
        (now, now, task_id),
    )
    await db.commit()
    await log_audit(
        db,
        action="task.delete",
        user=ctx.username,
        resource_type="task",
        resource_id=task_id,
        details={
            "project": row["project"],
            "title": row["title"],
            "status": row["status"],
        },
    )


async def reset_stale_tasks(
    ctx: CallerContext,
    db: aiosqlite.Connection,
    *,
    stale_days: int = 7,
) -> dict:
    """Reset stale in_progress tasks back to approved (operator+).

    Finds tasks with status='in_progress' and updated_at older than
    stale_days ago, resets them to 'approved', and adds a 'stale_reset' tag.
    Called by REM in its HYGIENE step.
    """
    require_role_ctx(ctx, "operator", "admin", "super_admin")

    from core.api.services.audit import log_audit

    now = datetime.now(timezone.utc).isoformat()
    stale_cursor = await db.execute(
        "SELECT id, tags FROM tasks "
        "WHERE status = 'in_progress' "
        "AND deleted_at IS NULL "
        "AND updated_at < datetime('now', ?)",
        (f"-{stale_days} days",),
    )
    stale_rows = await stale_cursor.fetchall()

    reset_count = 0
    reset_ids: list[str] = []
    for stale_row in stale_rows:
        sid = stale_row["id"]
        # Parse existing tags and add stale_reset
        try:
            existing_tags = json.loads(stale_row["tags"] or "[]")
        except (json.JSONDecodeError, TypeError):
            existing_tags = []
        if "stale_reset" not in existing_tags:
            existing_tags.append("stale_reset")
        tags_json = json.dumps(existing_tags)

        await db.execute(
            "UPDATE tasks SET status = 'approved', tags = ?, updated_at = ? WHERE id = ?",
            (tags_json, now, sid),
        )
        reset_count += 1
        reset_ids.append(sid)

    if reset_count > 0:
        await db.commit()
        await log_audit(
            db,
            action="task.stale_reset",
            user=ctx.username,
            resource_type="task",
            resource_id="batch",
            details={
                "stale_days": stale_days,
                "reset_count": reset_count,
                "task_ids": reset_ids[:20],  # limit audit detail size
            },
        )
        logger.info(
            "Stale reset: %d tasks reset from in_progress to approved (stale_days=%d)",
            reset_count,
            stale_days,
        )

    return {"reset_count": reset_count, "task_ids": reset_ids}


async def zombie_scan(
    ctx: CallerContext,
    db: aiosqlite.Connection,
    *,
    threshold_days: int = ZOMBIE_THRESHOLD_DAYS_DEFAULT,
    dry_run: bool = False,
) -> ZombieScanResponse:
    """Scan for aging zombie tasks and emit one Console notification per project (operator+).

    Zombie = approved + updated_at < now - threshold_days + no open/draft/merging PR
    + no 'dormant-ok' tag. One notification per project aggregates the task list.
    dry_run=true returns the report without writing notifications.
    """
    require_role_ctx(ctx, "operator", "admin", "super_admin")

    from core.api.services.audit import log_audit

    cutoff = (
        datetime.now(timezone.utc) - timedelta(days=threshold_days)
    ).isoformat()

    db.row_factory = aiosqlite.Row
    cursor = await db.execute(
        f"""
        SELECT id, title, project, tags, updated_at,
               {_PR_STATUS_SUBQUERY}
        FROM tasks
        WHERE status = 'approved'
          AND deleted_at IS NULL
          AND updated_at < ?
        """,
        (cutoff,),
    )
    rows = await cursor.fetchall()

    by_project: dict[str, list[dict]] = defaultdict(list)
    now_utc = datetime.now(timezone.utc)
    for row in rows:
        # Skip if opted out via dormant-ok tag
        try:
            tags = json.loads(row["tags"] or "[]") if row["tags"] else []
        except (json.JSONDecodeError, TypeError):
            tags = []
        if "dormant-ok" in tags:
            continue
        # Skip if an active PR exists (anti-zombie A handles this path)
        if row["pr_status"] in ("open", "draft", "merging"):
            continue

        try:
            upd = datetime.fromisoformat(
                (row["updated_at"] or "").replace("Z", "+00:00")
            )
            if upd.tzinfo is None:
                upd = upd.replace(tzinfo=timezone.utc)
            age_days = (now_utc - upd).days
        except ValueError:
            age_days = threshold_days  # fallback if malformed

        by_project[row["project"] or "unknown"].append(
            {
                "id": row["id"],
                "title": (row["title"] or "")[:100],
                "age_days": age_days,
            }
        )

    total = sum(len(v) for v in by_project.values())
    notifications_created = 0

    if not dry_run and total > 0:
        # Find admin recipients (one notification per project per admin)
        recipients_cursor = await db.execute(
            "SELECT id FROM users WHERE type = 'human' AND system_role IN ('admin', 'super_admin')"
        )
        recipients = await recipients_cursor.fetchall()

        for project, items in by_project.items():
            sorted_items = sorted(
                items, key=lambda x: x["age_days"], reverse=True
            )
            title = (
                f"{len(items)} task approved da >{threshold_days}gg in {project}"
            )
            body_json = json.dumps(
                {
                    "project": project,
                    "count": len(items),
                    "threshold_days": threshold_days,
                    "task_ids": [i["id"] for i in sorted_items],
                    "samples": sorted_items[:10],
                }
            )
            for recipient in recipients:
                try:
                    await db.execute(
                        "INSERT INTO notifications "
                        "(user_id, type, title, body, target_type, target_id, project) "
                        "VALUES (?, 'task_zombie_report', ?, ?, NULL, NULL, ?)",
                        (recipient["id"], title, body_json, project),
                    )
                    notifications_created += 1
                except aiosqlite.Error as exc:
                    logger.warning(
                        "zombie-scan: notification insert failed for user=%s project=%s: %s",
                        recipient["id"],
                        project,
                        exc,
                    )

        await db.commit()
        await log_audit(
            db,
            action="task.zombie_scan",
            user=ctx.username,
            resource_type="task",
            resource_id="batch",
            details={
                "threshold_days": threshold_days,
                "total_zombies": total,
                "projects": list(by_project.keys()),
                "notifications_created": notifications_created,
            },
        )
        logger.info(
            "zombie-scan: %d zombies across %d projects, %d notifications emitted",
            total,
            len(by_project),
            notifications_created,
        )

    return ZombieScanResponse(
        threshold_days=threshold_days,
        dry_run=dry_run,
        total_zombies=total,
        by_project=dict(by_project),
        notifications_created=notifications_created,
    )


async def bulk_reject_tasks(
    ctx: CallerContext,
    db: aiosqlite.Connection,
    *,
    task_ids: list[str],
    reason: str = "aging_zombie",
) -> BulkRejectResponse:
    """Bulk-reject multiple tasks with a reason (admin+).

    Validates each transition via VALID_TRANSITIONS (approved/pending/failed
    → rejected). Logs one audit entry per successful rejection plus a summary
    audit entry for the batch. Partial failures are reported, not raised.
    """
    require_role_ctx(ctx, "admin", "super_admin")

    from core.api.services.audit import log_audit

    rejected: list[str] = []
    failed: list[dict] = []
    now = datetime.now(timezone.utc).isoformat()

    db.row_factory = aiosqlite.Row
    for task_id in task_ids:
        try:
            cursor = await db.execute(
                "SELECT id, status, project, title, tags FROM tasks "
                "WHERE id = ? AND deleted_at IS NULL",
                (task_id,),
            )
            row = await cursor.fetchone()
            if not row:
                failed.append({"task_id": task_id, "error": "not_found"})
                continue

            allowed = VALID_TRANSITIONS.get(row["status"], set())
            if "rejected" not in allowed:
                failed.append(
                    {
                        "task_id": task_id,
                        "error": f"invalid_transition from {row['status']}",
                    }
                )
                continue

            # Append reason tag for traceability
            try:
                existing_tags = json.loads(row["tags"] or "[]")
            except (json.JSONDecodeError, TypeError):
                existing_tags = []
            reason_tag = f"rejected:{reason}"
            if reason_tag not in existing_tags:
                existing_tags.append(reason_tag)
            tags_json = json.dumps(existing_tags)

            await db.execute(
                "UPDATE tasks SET status = 'rejected', tags = ?, updated_at = ? WHERE id = ?",
                (tags_json, now, task_id),
            )
            rejected.append(task_id)

            await log_audit(
                db,
                action="task.bulk_reject",
                user=ctx.username,
                resource_type="task",
                resource_id=task_id,
                details={
                    "reason": reason,
                    "old_status": row["status"],
                    "project": row["project"],
                },
            )
        except Exception as exc:  # noqa: BLE001 — per-task isolation
            failed.append({"task_id": task_id, "error": str(exc)})

    if rejected:
        await db.commit()
        await log_audit(
            db,
            action="task.bulk_reject_batch",
            user=ctx.username,
            resource_type="task",
            resource_id="batch",
            details={
                "reason": reason,
                "rejected_count": len(rejected),
                "failed_count": len(failed),
                "rejected_ids_sample": rejected[:20],
            },
        )
        logger.info(
            "bulk-reject: %d tasks rejected, %d failed (reason=%s)",
            len(rejected),
            len(failed),
            reason,
        )

    return BulkRejectResponse(
        rejected=rejected,
        failed=failed,
        total=len(task_ids),
    )


async def trigger_reminder_check(ctx: CallerContext) -> dict:
    """Trigger manual reminder check (operator+). Called by cron or agent."""
    require_role_ctx(ctx, "operator", "admin", "super_admin")

    from core.api.services.reminder_service import check_and_send_reminders

    count = await check_and_send_reminders()
    return {"reminders_sent": count}
