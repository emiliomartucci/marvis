# v1.0.0 - 2026-05-27 - S1 F3.0: tasks MCP tool group (use_cases-direct, no HTTP)
"""Tasks MCP tools — port of the Node ``tasks`` group, calling use_cases DIRECTLY.

Replaces the Node proxy (``get``/``post``/``patch``/``del`` -> HTTP ``:8100``) with
an in-process ``await tasks_uc.<action>(LOCAL_CTX, db, ...)``. No uvicorn,
no fetch. Docstrings are copied VERBATIM from ``core/mcp-pir/index.mjs`` (they are
curated, carry the QUANDO USARLO / QUANDO NON USARLO / RESTITUISCE blocks).

Schema port (Zod -> Pydantic type hints), per S1 F3:
  * ``z.enum([...])``                -> ``Literal[...]``
  * ``z.string().min(1).max(N)``     -> ``Annotated[str, Field(min_length=, max_length=)]``
  * ``z.number().min(1).max(10).optional()`` -> ``Annotated[int, Field(ge=, le=)] | None = None``
  * optional                         -> ``X | None = None``
  * ``z.array(z.string())``          -> ``list[str]``

Return-typing decision (S1 F3 open question, decided): mutators (``create_task`` /
``update_task``) return the DTO ``.model_dump()`` (validated output schema); reads
return ``dict[str, Any]`` / ``list[dict]`` for a fast 1:1 port without fighting
``PydanticSchemaGenerationError`` on heterogeneous shapes. ``dump()`` (the adapter)
normalises both.

Visibility: the MCP surface is local single-user (no ``UserInfo.teams``), so every
tool passes ``visible_projects=None`` = unrestricted (DECISION 1 in the use_cases).

Seam callables for the mutators (``sync_graph`` / ``schedule_embed`` /
``requires_pr_gate``): the OSS MCP runtime is fastapi-free, so it must NOT import
``routers/tasks.py`` (which pulls fastapi for its HTTP test-seam machinery).
Instead it injects MCP-LOCAL seams from ``_adapter`` —
``mcp_schedule_embed`` (in-process fire-and-forget embed, S1 F4 — runs the SAME
fastapi-free ``embedding_service.embed_task_document`` the HTTP router uses) +
``mcp_requires_pr_gate``
(False: the PR-gate is a governance chokepoint that collapses in single-user OSS,
S1 §AUTH) — plus the genuinely shared fastapi-free ``graph_service.sync_task_to_graph``
for the KG node emit. This keeps the collapse honest: zero fastapi in the MCP path.
"""
from __future__ import annotations

from dataclasses import replace
from typing import Annotated, Any, Literal

from pydantic import Field

from core.api.mcp._adapter import (
    LOCAL_CTX,
    acquire_db,
    acquire_write_db,
    dump,
    mcp_requires_pr_gate,
    mcp_schedule_embed,
    raise_mcp_error,
)
from core.api.models.tasks import TaskCreateRequest, TaskUpdateRequest
from core.api.use_cases import tasks as tasks_uc
from core.api.use_cases._errors import ServiceError, ValidationError

# Literals shared across the tool signatures (mirror the Zod enums).
TaskKind = Literal["normal", "idea"]
TaskPriority = Literal["high", "medium", "low"]
TaskStatus = Literal[
    "pending", "approved", "in_progress", "completed", "rejected", "failed"
]
TaskSource = Literal["session", "manual", "console"]
TaskDelegation = Literal["agent", "hybrid", "human"]
TaskCompletionMode = Literal["pr", "doc", "none"]
TaskIdArg = Annotated[str, Field(min_length=1)]


def _resolve_task_id(*, id: str | None = None, task_id: str | None = None) -> str:
    """Accept both canonical `id` and legacy `task_id` tool arguments."""
    if id and task_id and id != task_id:
        raise ValidationError(
            code="ambiguous_task_id",
            message="Provide either id or task_id, not conflicting values",
        )
    resolved = id or task_id
    if not resolved:
        raise ValidationError(
            code="missing_task_id",
            message="Provide id or task_id",
        )
    return resolved


def _explicit_mcp_triage_ctx(action: str):
    """Return LOCAL_CTX with an explicit MCP triage grant for approval-gated writes."""
    return replace(LOCAL_CTX, delegation_grant_id=f"mcp:{action}")


def register(mcp) -> None:
    """Register the tasks tool group on the shared FastMCP instance."""

    @mcp.tool()
    async def list_tasks(
        project: str | None = None,
        status: str | None = None,
        kind: TaskKind | None = None,
        priority: str | None = None,
        program: str | None = None,
        limit: Annotated[int, Field(ge=1, le=100)] = 20,
    ) -> list[dict[str, Any]]:
        """Task rows with exact filters.

        QUANDO USARLO: ti servono titoli/status/PR state per project/status/priority/kind.
        QUANDO NON USARLO: conteggi -> tasks_summary; discovery semantica -> search; ID noto -> get_task.
        RESTITUISCE: list of {id, title, description, status, priority, project, ICE-D, tags, pr_state} paginato."""
        try:
            async with acquire_db() as db:
                # `program` is a Node-side filter not carried by the use_case
                # signature; preserved in the surface for parity, ignored here
                # (the Node proxy forwarded it as a query param the API also
                # ignored at this layer). visible_projects=None -> local sees all.
                result = await tasks_uc.list_tasks(
                    LOCAL_CTX,
                    db,
                    project=project,
                    status=status,
                    kind=kind,
                    priority=priority,
                    limit=limit,
                    visible_projects=None,
                )
                return dump(result)
        except ServiceError as e:
            raise_mcp_error(e)

    @mcp.tool()
    async def get_task(
        task_id: TaskIdArg | None = None,
        id: TaskIdArg | None = None,
        deep: bool | None = None,
    ) -> dict[str, Any]:
        """Get full detail of a single task by UUID.

        QUANDO USARLO: hai un task UUID (da list_tasks o da .task-style ref) e vuoi title/description/ICE-D/tags/PR linked. Usa ?deep=true per includere kg_context inline (neighbors, context_chain, applicable_learnings) — risparmia 2-3 tool call aggiuntivi. BOUNDARY: search vs get_task = NOT usare search per singolo artifact noto per ID -> usa get_task.
        QUANDO NON USARLO: NOT se hai solo filtri (project/status) -> usa list_tasks. NOT per ricerca semantica -> usa search.
        RESTITUISCE: {id, title, description, status, priority, project, ice_d, tags, pr_task_id} + kg_context se deep=true."""
        try:
            resolved_task_id = _resolve_task_id(id=id, task_id=task_id)
            async with acquire_db() as db:
                # DECISION 2: `deep` KG enrichment is an adapter concern; the
                # use_case returns kg_context=None. F3.0 ships the core fetch; the
                # deep-attach (build_kg_context_for_task) is a later F3 increment.
                result = await tasks_uc.get_task(
                    LOCAL_CTX, db, task_id=resolved_task_id, visible_projects=None
                )
                return dump(result)
        except ServiceError as e:
            raise_mcp_error(e)

    @mcp.tool()
    async def create_task(
        title: Annotated[str, Field(min_length=1, max_length=200)],
        project: Annotated[str, Field(min_length=1)],
        description: str | None = None,
        priority: TaskPriority = "medium",
        kind: TaskKind = "normal",
        source: TaskSource = "session",
        tags: list[str] | None = None,
        impact: Annotated[int, Field(ge=1, le=10)] | None = None,
        confidence: Annotated[int, Field(ge=1, le=10)] | None = None,
        ease: Annotated[int, Field(ge=1, le=10)] | None = None,
        delegation: TaskDelegation | None = None,
        completion_mode: TaskCompletionMode = "pr",
    ) -> dict[str, Any]:
        """Persist a new cross-session task in Marvis DB (survives session end, visible via MCP task tools and Console where available).

        QUANDO USARLO: PRIMA AZIONE per ogni lavoro implementativo (feat/fix/refactor/research) — richiesto da Constitution Rule 1 prima di qualunque Edit/Write. SEMPRE con ICE-D (impact/confidence/ease/delegation).
        QUANDO NON USARLO: NOT per modificare un task esistente -> usa update_task. NOT per free chat/brainstorm (session-first).
        PROVA: task pending persistito nel brain hosted; poi approve_task/update_task per lifecycle.
        RESTITUISCE: {id, title, status:pending, project, ...} — se resta pending, approva via approve_task oppure scarta via reject_task."""
        # Seam callables: the MCP surface is fastapi-free, so it does NOT import
        # the router. KG-sync reuses the shared fastapi-free graph_service; auto-embed
        # is the MCP-local no-op seam (full embedder is S1 F4). See _adapter.py.
        from core.api.services.graph_service import sync_task_to_graph

        body = TaskCreateRequest(
            title=title,
            project=project,
            description=description,
            priority=priority,
            kind=kind,
            source=source,
            tags=tags or [],
            impact=impact,
            confidence=confidence,
            ease=ease,
            delegation=delegation,
            completion_mode=completion_mode,
        )
        embed_jobs: list[dict[str, Any]] = []
        try:
            async with acquire_write_db(label="mcp.create_task") as db:
                result = await tasks_uc.create_task(
                    LOCAL_CTX,
                    db,
                    body=body,
                    created_by=LOCAL_CTX.username,
                    sync_graph=sync_task_to_graph,
                    schedule_embed=lambda **kw: embed_jobs.append(kw),
                )
            for job in embed_jobs:
                mcp_schedule_embed(**job)
            return dump(result)
        except ServiceError as e:
            raise_mcp_error(e)

    @mcp.tool()
    async def update_task(
        id: TaskIdArg | None = None,
        task_id: TaskIdArg | None = None,
        status: TaskStatus | None = None,
        kind: TaskKind | None = None,
        priority: TaskPriority | None = None,
        description: str | None = None,
        tags: list[str] | None = None,
        impact: Annotated[int, Field(ge=1, le=10)] | None = None,
        confidence: Annotated[int, Field(ge=1, le=10)] | None = None,
        ease: Annotated[int, Field(ge=1, le=10)] | None = None,
        delegation: TaskDelegation | None = None,
        completion_mode: TaskCompletionMode | None = None,
    ) -> dict[str, Any]:
        """Mutate an existing Marvis task (status, priority, description, tags, ICE-D, completion_mode).

        QUANDO USARLO: transizioni di stato lungo il lifecycle dopo approval (approved -> in_progress -> review -> completed) o rifinitura scoring/description dopo creazione.
        QUANDO NON USARLO: NOT per creare un nuovo task -> usa create_task. NOT per delete permanente -> usa delete_task. NOT per approvare: update_task(status='approved') e' BLOCCATO; usa approve_task per il flusso hosted/MCP.
        PROVA: record task aggiornato; usa get_task/get_pr per verificare stato e PR collegata.
        RESTITUISCE: task record aggiornato {id, title, status, ...}."""
        # Mirror the Node proxy: forward only the fields the caller actually set,
        # so TaskUpdateRequest.model_fields_set drives the "not sent vs sent null"
        # semantics the use_case relies on (it reads body.model_fields_set).
        provided: dict[str, Any] = {}
        for name, value in (
            ("status", status),
            ("kind", kind),
            ("priority", priority),
            ("description", description),
            ("tags", tags),
            ("impact", impact),
            ("confidence", confidence),
            ("ease", ease),
            ("delegation", delegation),
            ("completion_mode", completion_mode),
        ):
            if value is not None:
                provided[name] = value
        body = TaskUpdateRequest(**provided)
        embed_jobs: list[dict[str, Any]] = []
        try:
            resolved_task_id = _resolve_task_id(id=id, task_id=task_id)
            async with acquire_write_db(label="mcp.update_task") as db:
                result = await tasks_uc.update_task(
                    LOCAL_CTX,
                    db,
                    task_id=resolved_task_id,
                    body=body,
                    requires_pr_gate=mcp_requires_pr_gate,
                    schedule_embed=lambda **kw: embed_jobs.append(kw),
                )
            for job in embed_jobs:
                mcp_schedule_embed(**job)
            return dump(result)
        except ServiceError as e:
            raise_mcp_error(e)

    @mcp.tool()
    async def approve_task(
        task_id: TaskIdArg | None = None,
        id: TaskIdArg | None = None,
    ) -> dict[str, Any]:
        """Approve a pending task through explicit MCP triage.

        QUANDO USARLO: hosted/non-Console flow quando Emilio ha deciso di approvare un task pending e serve sbloccare in_progress/create_branch/register_branch. Questo tool usa una delegation grant MCP esplicita, quindi non richiede MARVIS_MCP_HUMAN_SESSION globale.
        QUANDO NON USARLO: NOT per modifiche ordinarie (priority/tags/description) -> usa update_task. NOT per task da scartare -> usa reject_task.
        PROVA: status='approved'; NEXT: update_task(in_progress) o create_branch.
        RESTITUISCE: task record aggiornato con status='approved'."""
        body = TaskUpdateRequest(status="approved")
        embed_jobs: list[dict[str, Any]] = []
        try:
            resolved_task_id = _resolve_task_id(id=id, task_id=task_id)
            async with acquire_write_db(label="mcp.approve_task") as db:
                result = await tasks_uc.update_task(
                    _explicit_mcp_triage_ctx("approve_task"),
                    db,
                    task_id=resolved_task_id,
                    body=body,
                    requires_pr_gate=mcp_requires_pr_gate,
                    schedule_embed=lambda **kw: embed_jobs.append(kw),
                )
            for job in embed_jobs:
                mcp_schedule_embed(**job)
            return dump(result)
        except ServiceError as e:
            raise_mcp_error(e)

    @mcp.tool()
    async def reject_task(
        task_id: TaskIdArg | None = None,
        id: TaskIdArg | None = None,
    ) -> dict[str, Any]:
        """Reject a pending task through explicit MCP triage.

        QUANDO USARLO: hosted/non-Console flow quando un task pending e' duplicato, rumore o non va lavorato. Preserva audit/history, a differenza di delete_task.
        QUANDO NON USARLO: NOT per approvare -> usa approve_task. NOT per cancellazione irreversibile di spam puro -> usa delete_task.
        RESTITUISCE: task record aggiornato con status='rejected'."""
        body = TaskUpdateRequest(status="rejected")
        embed_jobs: list[dict[str, Any]] = []
        try:
            resolved_task_id = _resolve_task_id(id=id, task_id=task_id)
            async with acquire_write_db(label="mcp.reject_task") as db:
                result = await tasks_uc.update_task(
                    _explicit_mcp_triage_ctx("reject_task"),
                    db,
                    task_id=resolved_task_id,
                    body=body,
                    requires_pr_gate=mcp_requires_pr_gate,
                    schedule_embed=lambda **kw: embed_jobs.append(kw),
                )
            for job in embed_jobs:
                mcp_schedule_embed(**job)
            return dump(result)
        except ServiceError as e:
            raise_mcp_error(e)

    @mcp.tool()
    async def delete_task(
        task_id: TaskIdArg | None = None,
        id: TaskIdArg | None = None,
    ) -> dict[str, Any]:
        """Permanently remove a task from the Marvis DB (non-recoverable).

        QUANDO USARLO: solo per spam/duplicate o entry chiaramente invalidi.
        QUANDO NON USARLO: NOT per abbandonare un task valido -> usa update_task con status='rejected' o 'failed' (preserva audit trail). Deletion cancella la history.
        RESTITUISCE: {deleted: true} o 404 se non esiste."""
        try:
            resolved_task_id = _resolve_task_id(id=id, task_id=task_id)
            async with acquire_write_db(label="mcp.delete_task") as db:
                await tasks_uc.delete_task(LOCAL_CTX, db, task_id=resolved_task_id)
                return {"deleted": True}
        except ServiceError as e:
            raise_mcp_error(e)

    @mcp.tool()
    async def tasks_summary() -> dict[str, Any]:
        """Task counts only, grouped by status and project.

        QUANDO USARLO: dashboard, health check, "quanti task".
        QUANDO NON USARLO: ti servono titoli o descrizioni -> list_tasks.
        RESTITUISCE: {by_status:{pending:N,...}, by_project:{slug:{pending:N,...}}}."""
        try:
            async with acquire_db() as db:
                result = await tasks_uc.get_tasks_summary(LOCAL_CTX, db)
                return dump(result)
        except ServiceError as e:
            raise_mcp_error(e)
