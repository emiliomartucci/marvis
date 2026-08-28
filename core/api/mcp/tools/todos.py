# v1.0.0 - 2026-06-12 - Todos MCP tool group
from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import Field

from core.api.mcp._adapter import (
    acquire_db,
    acquire_write_db,
    current_mcp_context,
    dump,
    mcp_schedule_embed,
    raise_mcp_error,
)
from core.api.models.todos import TodoCreateRequest, TodoDelegateRequest, TodoUpdateRequest
from core.api.services.graph_service import sync_task_to_graph
from core.api.use_cases import todos as todos_uc
from core.api.use_cases._errors import ServiceError

TodoType = Literal["promemoria", "azione", "idea", "decidi", "rivedi"]
TodoStatus = Literal[
    "aperto",
    "in_revisione",
    "fatto",
    "delegato",
    "scartato",
    "promosso",
    "deciso",
]
TodoSource = Literal["user", "agent", "brain"]
TodoDoer = Literal["human", "agent", "hybrid"]


def register(mcp) -> None:
    """Register the todos tool group on the shared FastMCP instance."""

    @mcp.tool()
    async def create_todo(
        text: Annotated[str, Field(min_length=1, max_length=5000)],
        type: TodoType | None = None,
        project: str | None = None,
        fu: Annotated[str, Field(pattern=r"^\d{4}-\d{2}-\d{2}$")] | None = None,
        source: TodoSource = "agent",
    ) -> dict[str, Any]:
        """Create a lightweight todo in the unified queue.

        QUANDO USARLO: catturare un promemoria/azione/idea/decisione che deve restare nella coda personale prima di diventare task.
        QUANDO NON USARLO: NOT per creare lavoro tracciato con PR/owner diretto -> usa create_task. NOT per approvare cio' che ha gia' una coda propria: task in review -> approve_task/reject_task, finding -> brain_findings_patch, memory-op -> brain_memory_operations_patch. Le PR e le loro approvazioni vivono su GitHub, fuori da questa superficie.
        RESTITUISCE: todo persistito {id,type,status,fu,project,doer,linked_task_id}."""
        body = TodoCreateRequest(
            text=text,
            type=type,
            project=project,
            fu=fu,
            source=source,
        )
        try:
            ctx = current_mcp_context()
            async with acquire_write_db(label="mcp.create_todo") as db:
                result = await todos_uc.create_todo(
                    ctx,
                    db,
                    body=body,
                    created_by=ctx.username,
                )
                return dump(result)
        except ServiceError as e:
            raise_mcp_error(e)

    @mcp.tool()
    async def list_todos(
        status: str | None = None,
        type: TodoType | Literal["approva"] | None = None,
        project: str | None = None,
        limit: Annotated[int, Field(ge=1, le=500)] = 100,
    ) -> list[dict[str, Any]]:
        """List todos with exact filters, including virtual approval items.

        QUANDO USARLO: vedere cosa aspetta l'utente, filtrando per status/type/project.
        QUANDO NON USARLO: NOT per enumerare task tracciati -> usa list_tasks. NOT per agire sugli approva virtuali: agisci sulla coda originale secondo origin.kind — task_review -> approve_task/reject_task (la PR sta su GitHub, non c'e' un tool PR qui); finding -> brain_findings_patch, poi brain_findings_apply che ritorna guidance e NON scrive; memory_op -> brain_memory_operations_patch, poi brain_memory_operations_apply (idem, solo guidance).
        RESTITUISCE: array di todo; gli approva virtuali hanno virtual=true e origin.kind (task_review|finding|memory_op)."""
        try:
            ctx = current_mcp_context()
            async with acquire_db() as db:
                result = await todos_uc.list_todos(
                    ctx,
                    db,
                    status=status,
                    type=type,
                    project=project,
                    limit=limit,
                    offset=0,
                )
                return dump(result)
        except ServiceError as e:
            raise_mcp_error(e)

    @mcp.tool()
    async def update_todo(
        id: Annotated[str, Field(min_length=1)],
        status: TodoStatus | None = None,
        fu: Annotated[str, Field(pattern=r"^\d{4}-\d{2}-\d{2}$")] | None = None,
        project: str | None = None,
        doer: TodoDoer | None = None,
        type: TodoType | None = None,
        text: str | None = None,
    ) -> dict[str, Any]:
        """Mutate a todo or move it through its type-specific state machine.

        QUANDO USARLO: completare/scartare/posticipare/riassegnare un todo, oppure promuovere/delegare quando la state machine lo consente.
        QUANDO NON USARLO: NOT per modificare approva virtuali: agisci sulla coda originale (approve_task/reject_task, brain_findings_patch, brain_memory_operations_patch). NOT per creare task manuali non derivati dal todo -> usa create_task.
        RESTITUISCE: todo aggiornato con eventuale linked_task_id."""
        body = TodoUpdateRequest(
            **{
                k: v
                for k, v in {
                    "status": status,
                    "fu": fu,
                    "project": project,
                    "doer": doer,
                    "type": type,
                    "text": text,
                }.items()
                if v is not None
            }
        )
        embed_jobs: list[dict[str, Any]] = []
        try:
            ctx = current_mcp_context()
            async with acquire_write_db(label="mcp.update_todo") as db:
                result = await todos_uc.update_todo(
                    ctx,
                    db,
                    todo_id=id,
                    body=body,
                    sync_graph=sync_task_to_graph,
                    schedule_embed=lambda **kw: embed_jobs.append(kw),
                )
            for job in embed_jobs:
                mcp_schedule_embed(**job)
            return dump(result)
        except ServiceError as e:
            raise_mcp_error(e)

    @mcp.tool()
    async def delegate_todo(
        id: Annotated[str, Field(min_length=1)],
        project: str | None = None,
        title: Annotated[str, Field(min_length=1, max_length=200)] | None = None,
    ) -> dict[str, Any]:
        """Delegate a todo into a real tracked task.

        QUANDO USARLO: trasformare un todo di tipo azione/rivedi con doer agent/hybrid in un Task reale idempotente.
        QUANDO NON USARLO: NOT se doer=human. NOT per idee: usa update_todo status=promosso.
        RESTITUISCE: todo in stato delegato con linked_task_id valorizzato."""
        embed_jobs: list[dict[str, Any]] = []
        try:
            ctx = current_mcp_context()
            async with acquire_write_db(label="mcp.delegate_todo") as db:
                result = await todos_uc.delegate_todo(
                    ctx,
                    db,
                    todo_id=id,
                    body=TodoDelegateRequest(project=project, title=title),
                    sync_graph=sync_task_to_graph,
                    schedule_embed=lambda **kw: embed_jobs.append(kw),
                )
            for job in embed_jobs:
                mcp_schedule_embed(**job)
            return dump(result)
        except ServiceError as e:
            raise_mcp_error(e)
