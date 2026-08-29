# v1.0.0 - 2026-06-12 - Todos use cases
"""Todos use_cases - transport-agnostic backend for the unified queue."""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

import aiosqlite

from core.api.models.tasks import TaskCreateRequest
from core.api.models.todos import (
    PERSISTED_TODO_TYPES,
    TERMINAL_TODO_STATUSES,
    VALID_TODO_TRANSITIONS,
    TodoDelegateRequest,
    TodoResponse,
)
from core.api.use_cases import tasks as tasks_uc
from core.api.use_cases._context import (
    CallerContext,
    require_role_ctx,
    require_workspace_ctx,
)
from core.api.use_cases._errors import (
    AuthorizationError,
    ConflictError,
    NotFoundError,
    ValidationError,
)

logger = logging.getLogger(__name__)

_bg_classify_tasks: set[asyncio.Task] = set()


def _today() -> str:
    return datetime.now().date().isoformat()


def _tomorrow() -> str:
    return (datetime.now().date() + timedelta(days=1)).isoformat()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _default_fu(todo_type: str) -> str:
    return _tomorrow() if todo_type == "idea" else _today()


def _loads_payload(raw: str | None) -> dict[str, Any] | None:
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else {"value": value}


def _dumps_payload(payload: dict[str, Any] | None) -> str | None:
    if payload is None:
        return None
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _row_to_todo(row: aiosqlite.Row) -> TodoResponse:
    return TodoResponse(
        id=row["id"],
        type=row["type"],
        family=row["family"],
        status=row["status"],
        text=row["text"],
        payload=_loads_payload(row["payload"]),
        fu=row["fu"],
        project=row["project"],
        source=row["source"],
        source_ref=row["source_ref"],
        doer=row["doer"],
        linked_task_id=row["linked_task_id"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        resolved_at=row["resolved_at"],
    )


def _todo_not_found(todo_id: str) -> NotFoundError:
    return NotFoundError(
        code="todo_not_found",
        message=f"Todo not found (id={todo_id!r})",
    )


def _transition_action(new_status: str) -> str:
    if new_status == "fatto":
        return "todo.complete"
    if new_status == "scartato":
        return "todo.discard"
    if new_status == "delegato":
        return "todo.delegate"
    if new_status == "promosso":
        return "todo.promote"
    if new_status in {"deciso", "in_revisione"}:
        return "todo.decide"
    return "todo.decide"


async def _fetch_todo(
    db: aiosqlite.Connection,
    todo_id: str,
    *,
    workspace_id: str,
) -> aiosqlite.Row:
    db.row_factory = aiosqlite.Row
    row = await (
        await db.execute(
            "SELECT * FROM todos WHERE id = ? AND workspace_id = ?",
            (todo_id, workspace_id),
        )
    ).fetchone()
    if row is None:
        raise _todo_not_found(todo_id)
    return row


def _virtual_todo(
    *,
    id: str,
    origin_kind: str,
    text: str,
    payload: dict[str, Any],
    project: str | None,
    created_at: str | None,
    updated_at: str | None,
) -> TodoResponse:
    from core.api.models.todos import TodoOrigin

    now = _now()
    return TodoResponse(
        id=f"virtual:{origin_kind}:{id}",
        type="approva",
        family="system",
        status="aperto",
        text=text,
        payload=payload,
        fu=_today(),
        project=project,
        source="brain" if origin_kind in {"finding", "memory_op"} else "agent",
        source_ref=id,
        doer="human",
        linked_task_id=None,
        created_at=created_at or now,
        updated_at=updated_at or created_at or now,
        resolved_at=None,
        virtual=True,
        origin=TodoOrigin(kind=origin_kind, id=id),
    )


def _passes_virtual_filters(
    todo: TodoResponse,
    *,
    status: str | None,
    todo_type: str | None,
    project: str | None,
) -> bool:
    if status and "aperto" not in {s.strip() for s in status.split(",")}:
        return False
    if todo_type and "approva" not in {t.strip() for t in todo_type.split(",")}:
        return False
    if project and todo.project != project:
        return False
    return True


async def _list_virtual_approvals(
    db: aiosqlite.Connection,
    *,
    workspace_id: str,
    status: str | None,
    todo_type: str | None,
    project: str | None,
) -> list[TodoResponse]:
    items: list[TodoResponse] = []

    task_rows = await (
        await db.execute(
            "SELECT t.id AS task_id, t.title, t.description, t.project, "
            "t.created_at, t.updated_at, pr.id AS pr_id, pr.status AS pr_status, "
            "pr.branch, pr.title AS pr_title "
            "FROM tasks t "
            "JOIN pull_requests pr ON pr.task_id = t.id "
            "AND pr.status IN ('draft', 'open', 'merging') "
            "AND pr.workspace_id = t.workspace_id "
            "WHERE t.status = 'review' AND t.deleted_at IS NULL "
            "AND t.workspace_id = ? "
            "ORDER BY t.updated_at DESC",
            (workspace_id,),
        )
    ).fetchall()
    for row in task_rows:
        item = _virtual_todo(
            id=row["task_id"],
            origin_kind="task_review",
            text=f"Approva task: {row['title']}",
            payload={
                "task_id": row["task_id"],
                "pr_id": row["pr_id"],
                "pr_status": row["pr_status"],
                "branch": row["branch"],
                "title": row["pr_title"] or row["title"],
                "description": row["description"],
            },
            project=row["project"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
        if _passes_virtual_filters(
            item, status=status, todo_type=todo_type, project=project
        ):
            items.append(item)

    finding_rows = await (
        await db.execute(
            "SELECT f.finding_id, f.title, f.summary, f.scope_type, f.scope_key, "
            "f.severity, f.confidence, f.approval_state, f.created_at, f.updated_at "
            "FROM brain_findings f "
            "JOIN brain_runs r ON r.run_id = f.run_id "
            "WHERE f.approval_state IN ('open', 'pending_bootstrap') "
            "AND f.superseded_by_finding_id IS NULL "
            "AND r.workspace_id = ? "
            "ORDER BY f.created_at DESC",
            (workspace_id,),
        )
    ).fetchall()
    for row in finding_rows:
        item = _virtual_todo(
            id=row["finding_id"],
            origin_kind="finding",
            text=f"Approva finding: {row['title']}",
            payload={
                "finding_id": row["finding_id"],
                "summary": row["summary"],
                "severity": row["severity"],
                "confidence": row["confidence"],
                "approval_state": row["approval_state"],
            },
            project=row["scope_key"] if row["scope_type"] == "project" else None,
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
        if _passes_virtual_filters(
            item, status=status, todo_type=todo_type, project=project
        ):
            items.append(item)

    memory_rows = await (
        await db.execute(
            "SELECT o.operation_id, o.operation_type, o.summary, o.scope_type, "
            "o.scope_key, o.score, o.approval_state, o.created_at, o.updated_at, "
            "o.proposed_write_json "
            "FROM brain_memory_operations o "
            "JOIN brain_runs r ON r.run_id = o.run_id "
            "WHERE o.approval_state = 'pending' "
            "AND o.superseded_by_operation_id IS NULL "
            "AND r.workspace_id = ? "
            "ORDER BY o.score DESC, o.created_at DESC",
            (workspace_id,),
        )
    ).fetchall()
    for row in memory_rows:
        item = _virtual_todo(
            id=row["operation_id"],
            origin_kind="memory_op",
            text=f"Approva memoria: {row['summary']}",
            payload={
                "operation_id": row["operation_id"],
                "operation_type": row["operation_type"],
                "summary": row["summary"],
                "score": row["score"],
                "approval_state": row["approval_state"],
                "proposed_write": _loads_payload(row["proposed_write_json"]),
            },
            project=row["scope_key"] if row["scope_type"] == "project" else None,
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
        if _passes_virtual_filters(
            item, status=status, todo_type=todo_type, project=project
        ):
            items.append(item)

    return items


async def list_todos(
    ctx: CallerContext,
    db: aiosqlite.Connection,
    *,
    status: str | None = None,
    type: str | None = None,
    project: str | None = None,
    limit: int = 100,
    offset: int = 0,
    include_virtual: bool = True,
) -> list[TodoResponse]:
    """List persisted todos plus read-only virtual approval projections."""
    require_role_ctx(ctx, "viewer", "operator", "admin", "super_admin")
    workspace_id = require_workspace_ctx(ctx)
    db.row_factory = aiosqlite.Row
    conditions: list[str] = ["workspace_id = ?"]
    params: list[Any] = [workspace_id]

    if status:
        statuses = [s.strip() for s in status.split(",") if s.strip()]
        placeholders = ",".join("?" for _ in statuses)
        conditions.append(f"status IN ({placeholders})")
        params.extend(statuses)
    if type:
        types = [t.strip() for t in type.split(",") if t.strip()]
        placeholders = ",".join("?" for _ in types)
        conditions.append(f"type IN ({placeholders})")
        params.extend(types)
    if project:
        conditions.append("project = ?")
        params.append(project)

    where = " AND ".join(conditions) if conditions else "1=1"
    rows = await (
        await db.execute(
            f"SELECT * FROM todos WHERE {where} "
            "ORDER BY fu ASC, created_at DESC",
            params,
        )
    ).fetchall()
    items = [_row_to_todo(row) for row in rows]
    if include_virtual:
        items.extend(
            await _list_virtual_approvals(
                db,
                workspace_id=workspace_id,
                status=status,
                todo_type=type,
                project=project,
            )
        )
    items.sort(key=lambda item: (item.fu, item.created_at))
    return items[offset : offset + limit]


def _schedule_classify(
    *,
    todo_id: str,
    workspace_id: str,
    text: str,
    original_updated_at: str,
    missing_fields: set[str],
) -> None:
    async def _runner() -> None:
        await _classify_and_update(
            todo_id=todo_id,
            workspace_id=workspace_id,
            text=text,
            original_updated_at=original_updated_at,
            missing_fields=missing_fields,
        )

    try:
        task = asyncio.create_task(_runner())
    except RuntimeError:
        logger.debug("todo classify skipped: no running event loop")
        return
    _bg_classify_tasks.add(task)
    task.add_done_callback(_bg_classify_tasks.discard)


def _heuristic_project_candidates() -> list[tuple[str, str | None]]:
    """(slug, name) candidates from the canonical project index.

    Same source the rest of the API uses to resolve projects (TTL-cached
    index built from project dirs + project.yaml). Fail-soft: an empty list
    simply disables project auto-linking in the heuristic fallback.
    """
    try:
        import time as _time

        from core.api.routers import projects as projects_router

        if _time.monotonic() - projects_router._index_built_at > projects_router._INDEX_TTL:
            projects_router._build_project_index()
        candidates: list[tuple[str, str | None]] = []
        for slug, entry in projects_router._project_index.items():
            yaml_data = projects_router._read_project_yaml(entry.metadata_path) or {}
            name = yaml_data.get("name")
            candidates.append((slug, str(name) if name else None))
        return candidates
    except Exception:  # noqa: BLE001 - heuristic fallback is fail-soft
        logger.debug("heuristic project candidates unavailable", exc_info=True)
        return []


async def _classify_and_update(
    *,
    todo_id: str,
    workspace_id: str,
    text: str,
    original_updated_at: str,
    missing_fields: set[str],
) -> None:
    try:
        from core.api.db import acquire_db, acquire_write_db
        from core.api.services.todos.llm.factory import get_classifier

        # BYOK (gh #22 round 2): a DB-configured 'classify' provider — the user's
        # own key, managed on the Console BYOK page — takes precedence over the
        # env gateway, so a user-supplied key actually drives classification. Same
        # resolver the ingest classifier uses (single config surface). Fail-soft:
        # any resolution error falls through to the env gateway / heuristic.
        classifier = None
        try:
            from core.api.services.ingest.llm.config_store import resolve_function_provider
            from core.api.services.todos.llm.byok_provider import build_todo_classifier

            async with acquire_db() as cfg_db:
                resolved = await resolve_function_provider(
                    cfg_db, "classify", workspace_id
                )
            classifier = build_todo_classifier(resolved)
        except Exception:  # noqa: BLE001 - BYOK is optional; never block classify
            logger.debug("todos BYOK classify resolution failed", exc_info=True)
        if classifier is None:
            classifier = get_classifier()

        result = None
        if classifier is not None:
            result = await classifier.classify(
                text,
                {"todo_id": todo_id, "today": _today()},
            )
        # Marker (gh #22): record HOW the todo was classified so the console can
        # show a discreet "basic classification" disclaimer on heuristic results.
        classified_by = "llm" if result is not None else "heuristic"
        if result is None:
            # No LLM result (no provider / missing key / classifier failure):
            # fall back to the deterministic heuristic so the todo is never left
            # raw (issue #22). The missing key is surfaced via the honest banner.
            from core.api.services.todos.heuristics import heuristic_classify

            result = heuristic_classify(
                text,
                _today(),
                _heuristic_project_candidates(),
            )
        if result is None:
            return

        updates: dict[str, Any] = {}
        if "type" in missing_fields and result.type in PERSISTED_TODO_TYPES:
            updates["type"] = result.type
        if "project" in missing_fields and result.project_slug:
            updates["project"] = result.project_slug
        if "fu" in missing_fields:
            updates["fu"] = result.fu_date or _default_fu(result.type)
        if "doer" in missing_fields and result.doer:
            updates["doer"] = result.doer
        if not updates:
            return

        now = _now()
        async with acquire_write_db(label="todos.classify") as db:
            cur = await db.execute(
                "SELECT payload FROM todos "
                "WHERE id = ? AND workspace_id = ? AND updated_at = ?",
                (todo_id, workspace_id, original_updated_at),
            )
            row = await cur.fetchone()
            if row is None:
                # Changed under us (optimistic lock) — skip, don't clobber.
                return
            payload = _loads_payload(row[0]) or {}
            payload["classified_by"] = classified_by
            updates["payload"] = _dumps_payload(payload)

            assignments = [f"{field} = ?" for field in updates]
            params = list(updates.values())
            params.extend([now, todo_id, workspace_id, original_updated_at])
            await db.execute(
                f"UPDATE todos SET {', '.join(assignments)}, updated_at = ? "
                "WHERE id = ? AND workspace_id = ? AND updated_at = ?",
                params,
            )
            await db.commit()
    except Exception:  # noqa: BLE001 - background classify is fail-soft
        logger.warning("todo classify background update failed", exc_info=True)


async def create_todo(
    ctx: CallerContext,
    db: aiosqlite.Connection,
    *,
    body,
    created_by: str,
    schedule_classify: bool = True,
) -> TodoResponse:
    require_role_ctx(ctx, "operator", "admin", "super_admin")
    workspace_id = require_workspace_ctx(ctx)
    from core.api.services.audit import log_audit
    from core.api.services.access_grants import require_workspace_project_bound

    if body.project:
        await require_workspace_project_bound(db, ctx, body.project)

    todo_type = body.type or "promemoria"
    if todo_type == "approva":
        raise ValidationError(
            code="approva_is_virtual",
            message="type='approva' is read-only and projected from existing approval queues.",
        )
    if todo_type not in PERSISTED_TODO_TYPES:
        raise ValidationError(code="invalid_todo_type", message=f"Invalid type: {todo_type}")

    todo_id = str(uuid.uuid4())
    now = _now()
    fu = body.fu or _default_fu(todo_type)
    family = (
        "system"
        if body.source == "brain" or todo_type in {"decidi", "rivedi"}
        else "captured"
    )
    missing_fields = {
        name
        for name in ("type", "project", "fu", "doer")
        if name not in body.model_fields_set
    }

    try:
        await db.execute(
            "INSERT INTO todos (id, type, family, status, text, payload, fu, project, "
            "source, source_ref, doer, linked_task_id, created_at, updated_at, resolved_at, "
            "workspace_id) "
            "VALUES (?, ?, ?, 'aperto', ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, NULL, ?)",
            (
                todo_id,
                todo_type,
                family,
                body.text,
                _dumps_payload(body.payload),
                fu,
                body.project,
                body.source,
                body.source_ref,
                body.doer,
                now,
                now,
                workspace_id,
            ),
        )
    except aiosqlite.IntegrityError:
        raise ConflictError(
            code="duplicate_todo",
            message="Duplicate todo: source and source_ref already exist.",
        )

    await log_audit(
        db,
        action="todo.create",
        user=created_by,
        resource_type="todo",
        resource_id=todo_id,
        details={
            "type": todo_type,
            "fu": fu,
            "project": body.project,
            "source": body.source,
            "source_ref": body.source_ref,
        },
        workspace_id=workspace_id,
    )
    await db.commit()

    row = await _fetch_todo(db, todo_id, workspace_id=workspace_id)
    if schedule_classify:
        _schedule_classify(
            todo_id=todo_id,
            workspace_id=workspace_id,
            text=body.text,
            original_updated_at=now,
            missing_fields=missing_fields,
        )
    return _row_to_todo(row)


async def _existing_task_id_for_todo(
    db: aiosqlite.Connection,
    todo_id: str,
    *,
    workspace_id: str,
) -> str | None:
    row = await (
        await db.execute(
            "SELECT id FROM tasks WHERE source = 'todo' AND source_ref = ? "
            "AND deleted_at IS NULL "
            "AND workspace_id = ? "
            "ORDER BY created_at ASC LIMIT 1",
            (todo_id, workspace_id),
        )
    ).fetchone()
    return row["id"] if row else None


async def _create_task_from_todo(
    ctx: CallerContext,
    db: aiosqlite.Connection,
    *,
    row: aiosqlite.Row,
    project: str,
    title: str | None,
    kind: str,
    sync_graph: Callable[..., Awaitable[bool]],
    schedule_embed: Callable[..., None],
) -> str:
    workspace_id = require_workspace_ctx(ctx)
    task_title = (title or row["text"]).strip()[:200]
    body = TaskCreateRequest(
        title=task_title,
        description=f"Created from todo {row['id']}.\n\n{row['text']}",
        project=project,
        kind=kind,
        priority="medium",
        source="todo",
        source_ref=row["id"],
        tags=["todo"],
        impact=5,
        confidence=6,
        ease=6,
        delegation="hybrid" if row["doer"] == "hybrid" else "agent",
        completion_mode="pr",
    )
    try:
        task = await tasks_uc.create_task(
            ctx,
            db,
            body=body,
            created_by=ctx.username,
            sync_graph=sync_graph,
            schedule_embed=schedule_embed,
        )
        return task.id
    except ConflictError:
        await db.rollback()
        existing = await _existing_task_id_for_todo(
            db,
            row["id"],
            workspace_id=workspace_id,
        )
        if existing:
            return existing
        raise


async def delegate_todo(
    ctx: CallerContext,
    db: aiosqlite.Connection,
    *,
    todo_id: str,
    body: TodoDelegateRequest | None,
    sync_graph: Callable[..., Awaitable[bool]],
    schedule_embed: Callable[..., None],
) -> TodoResponse:
    require_role_ctx(ctx, "operator", "admin", "super_admin")
    workspace_id = require_workspace_ctx(ctx)
    from core.api.services.audit import log_audit

    row = await _fetch_todo(db, todo_id, workspace_id=workspace_id)
    if row["type"] not in {"azione", "rivedi"}:
        raise ValidationError(
            code="todo_not_delegable",
            message="Only azione and rivedi todos can be delegated.",
        )
    if row["doer"] == "human":
        raise AuthorizationError(
            code="todo_delegation_requires_agent",
            message="Cannot delegate a todo whose doer is human.",
        )
    if row["status"] == "delegato" and row["linked_task_id"]:
        return _row_to_todo(row)
    if row["status"] != "aperto" and row["status"] != "in_revisione":
        raise ValidationError(
            code="invalid_transition",
            message=f"Cannot delegate todo from status {row['status']}.",
        )

    project = (body.project if body else None) or row["project"]
    if not project:
        raise ValidationError(
            code="todo_project_required",
            message="Delegating a todo requires a project.",
        )
    task_id = await _create_task_from_todo(
        ctx,
        db,
        row=row,
        project=project,
        title=body.title if body else None,
        kind="normal",
        sync_graph=sync_graph,
        schedule_embed=schedule_embed,
    )
    now = _now()
    await db.execute(
        "UPDATE todos SET status = 'delegato', project = ?, linked_task_id = ?, "
        "updated_at = ?, resolved_at = ? WHERE id = ? AND workspace_id = ?",
        (project, task_id, now, now, todo_id, workspace_id),
    )
    await log_audit(
        db,
        action="todo.delegate",
        user=ctx.username,
        resource_type="todo",
        resource_id=todo_id,
        details={"from_status": row["status"], "to_status": "delegato", "task_id": task_id},
        workspace_id=workspace_id,
    )
    await db.commit()
    return _row_to_todo(
        await _fetch_todo(db, todo_id, workspace_id=workspace_id)
    )


async def _promote_idea(
    ctx: CallerContext,
    db: aiosqlite.Connection,
    *,
    row: aiosqlite.Row,
    project: str | None,
    sync_graph: Callable[..., Awaitable[bool]],
    schedule_embed: Callable[..., None],
) -> TodoResponse:
    from core.api.services.audit import log_audit
    workspace_id = require_workspace_ctx(ctx)

    if row["status"] == "promosso" and row["linked_task_id"]:
        return _row_to_todo(row)
    if row["status"] != "aperto":
        raise ValidationError(
            code="invalid_transition",
            message=f"Cannot promote todo from status {row['status']}.",
        )

    target_project = project or row["project"]
    if not target_project:
        raise ValidationError(
            code="todo_project_required",
            message="Promoting an idea requires a project.",
        )
    task_id = await _create_task_from_todo(
        ctx,
        db,
        row=row,
        project=target_project,
        title=None,
        kind="idea",
        sync_graph=sync_graph,
        schedule_embed=schedule_embed,
    )
    now = _now()
    await db.execute(
        "UPDATE todos SET status = 'promosso', project = ?, linked_task_id = ?, "
        "updated_at = ?, resolved_at = ? WHERE id = ? AND workspace_id = ?",
        (target_project, task_id, now, now, row["id"], workspace_id),
    )
    await log_audit(
        db,
        action="todo.promote",
        user=ctx.username,
        resource_type="todo",
        resource_id=row["id"],
        details={
            "from_status": row["status"],
            "to_status": "promosso",
            "task_id": task_id,
        },
        workspace_id=workspace_id,
    )
    await db.commit()
    return _row_to_todo(
        await _fetch_todo(db, row["id"], workspace_id=workspace_id)
    )


async def update_todo(
    ctx: CallerContext,
    db: aiosqlite.Connection,
    *,
    todo_id: str,
    body,
    sync_graph: Callable[..., Awaitable[bool]],
    schedule_embed: Callable[..., None],
) -> TodoResponse:
    require_role_ctx(ctx, "operator", "admin", "super_admin")
    workspace_id = require_workspace_ctx(ctx)
    from core.api.services.audit import log_audit

    row = await _fetch_todo(db, todo_id, workspace_id=workspace_id)
    if body.status == "delegato":
        return await delegate_todo(
            ctx,
            db,
            todo_id=todo_id,
            body=TodoDelegateRequest(project=body.project),
            sync_graph=sync_graph,
            schedule_embed=schedule_embed,
        )
    if body.status == "promosso":
        if row["type"] != "idea":
            raise ValidationError(
                code="invalid_transition",
                message="Only idea todos can be promoted.",
            )
        return await _promote_idea(
            ctx,
            db,
            row=row,
            project=body.project,
            sync_graph=sync_graph,
            schedule_embed=schedule_embed,
        )

    updates: dict[str, Any] = {}
    audits: list[tuple[str, dict[str, Any]]] = []
    now = _now()

    if body.status and body.status != row["status"]:
        allowed = VALID_TODO_TRANSITIONS.get(row["type"], {}).get(row["status"], set())
        if body.status not in allowed:
            raise ValidationError(
                code="invalid_transition",
                message=(
                    f"Invalid transition: {row['type']} {row['status']} -> {body.status}. "
                    f"Allowed: {', '.join(sorted(allowed)) if allowed else 'none'}"
                ),
            )
        updates["status"] = body.status
        if body.status in TERMINAL_TODO_STATUSES:
            updates["resolved_at"] = now
        audits.append(
            (
                _transition_action(body.status),
                {"from_status": row["status"], "to_status": body.status},
            )
        )

    if "type" in body.model_fields_set and body.type and body.type != row["type"]:
        if body.type == "approva":
            raise ValidationError(
                code="approva_is_virtual",
                message="type='approva' is read-only and projected from approval queues.",
            )
        if body.type not in PERSISTED_TODO_TYPES:
            raise ValidationError(code="invalid_todo_type", message=f"Invalid type: {body.type}")
        updates["type"] = body.type
        audits.append(
            (
                "todo.reclassify",
                {"from_type": row["type"], "to_type": body.type},
            )
        )

    if "text" in body.model_fields_set and body.text is not None:
        updates["text"] = body.text
    if "payload" in body.model_fields_set:
        updates["payload"] = _dumps_payload(body.payload)
    if "fu" in body.model_fields_set and body.fu is not None and body.fu != row["fu"]:
        updates["fu"] = body.fu
        audits.append(("todo.postpone", {"from_fu": row["fu"], "to_fu": body.fu}))
    if "project" in body.model_fields_set and body.project != row["project"]:
        if body.project:
            from core.api.services.access_grants import (
                require_workspace_project_bound,
            )

            await require_workspace_project_bound(db, ctx, body.project)
        updates["project"] = body.project
        audits.append(
            ("todo.reassign", {"from_project": row["project"], "to_project": body.project})
        )
    if "doer" in body.model_fields_set and body.doer != row["doer"]:
        updates["doer"] = body.doer
        audits.append(
            (
                "todo.assign",
                {"from_doer": row["doer"], "to_doer": body.doer},
            )
        )

    if not updates:
        return _row_to_todo(row)

    updates["updated_at"] = now
    assignments = [f"{field} = ?" for field in updates]
    params = list(updates.values())
    params.extend([todo_id, workspace_id])
    # A filesystem ADR cannot share SQLite's transaction. The decision,
    # transition audit, and durable intent commit together first; the ADR is
    # then written and a correlated final receipt is appended separately.
    is_decidi_confirmation = (
        row["type"] == "decidi" and updates.get("status") == "deciso"
    )
    adr_context: dict[str, Any] | None = None
    adr_correlation_id: str | None = None
    if is_decidi_confirmation:
        effective_project = updates.get("project", row["project"])
        if "payload" in updates:
            effective_payload = _loads_payload(updates["payload"]) or {}
        else:
            effective_payload = _loads_payload(row["payload"]) or {}

        # gh #29 — the ADR context falls back to the todo text when the confirmer
        # (e.g. an API/MCP caller) did not supply a `domanda`, so the artefact is
        # never a generic "Decisione" with no context.
        if not str(effective_payload.get("domanda") or "").strip():
            effective_payload["domanda"] = row["text"]

        adr_correlation_id = uuid.uuid4().hex
        adr_context = {
            "todo_id": todo_id,
            "project": effective_project,
            "payload": effective_payload,
        }

    await db.execute(
        f"UPDATE todos SET {', '.join(assignments)} "
        "WHERE id = ? AND workspace_id = ?",
        params,
    )

    for action, details in audits:
        await log_audit(
            db,
            action=action,
            user=ctx.username,
            resource_type="todo",
            resource_id=todo_id,
            details=details,
            workspace_id=workspace_id,
        )

    if adr_context is not None and adr_correlation_id is not None:
        await log_audit(
            db,
            action="todo.decidi.intent",
            user=ctx.username,
            resource_type="todo",
            resource_id=todo_id,
            details={
                "todo_id": todo_id,
                "project": adr_context["project"],
                "from_status": row["status"],
                "to_status": "deciso",
                "correlation_id": adr_correlation_id,
            },
            workspace_id=workspace_id,
        )
    await db.commit()

    if adr_context is not None and adr_correlation_id is not None:
        from core.api.services.todos.adr import write_adr_guarded

        final_details = {
            "todo_id": todo_id,
            "project": adr_context["project"],
            "correlation_id": adr_correlation_id,
            "adr_written": False,
            "adr_name": None,
        }
        try:
            adr_path = await write_adr_guarded(
                ctx=ctx,
                db=db,
                project_slug=adr_context["project"],
                payload=adr_context["payload"],
                decisore=ctx.username,
                now=datetime.now(timezone.utc),
            )
            final_details["adr_written"] = adr_path is not None
            final_details["adr_name"] = (
                getattr(adr_path, "name", None) if adr_path is not None else None
            )
        except (OSError, PermissionError) as exc:
            logger.warning(
                "decidi gate ADR write failed for todo=%s project=%s: %s",
                todo_id,
                adr_context["project"],
                exc,
            )
            final_details["failure_type"] = type(exc).__name__
        await db.execute("BEGIN IMMEDIATE")
        await log_audit(
            db,
            action="todo.decidi.confirmed",
            user=ctx.username,
            resource_type="todo",
            resource_id=todo_id,
            details=final_details,
            workspace_id=workspace_id,
        )
        await db.commit()
    return _row_to_todo(
        await _fetch_todo(db, todo_id, workspace_id=workspace_id)
    )
