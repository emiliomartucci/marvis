# v1.0.0 - 2026-07-03 - P1 F2: cross-agent task comments MCP tools (comment_task + list_comments)
"""MCP tools — cross-agent task comments.

``comment_task`` / ``list_comments`` are the agent-to-agent collaboration surface:
one agent leaves a question/blocker on a task, another (with a grant on the task's
project) reads it and replies, and the task owner is notified. RBAC, redaction and
the notify producer all live in the shared ``use_cases.comments`` so the REST
router and these tools never diverge. Attribution is per-request via
``current_mcp_context()`` (the OAuth person's username, or ``<tenant>:static-bearer``
for the tenant bearer) — never ``LOCAL_CTX``.
"""
from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import Field

from core.api.mcp._adapter import (
    acquire_db,
    acquire_write_db,
    current_mcp_context,
    dump,
    raise_mcp_error,
)
from core.api.use_cases import comments as comments_uc
from core.api.use_cases._errors import ServiceError

CommentStatus = Literal["info", "question", "blocker", "resolved"]


def register(mcp) -> None:
    """Register the comments tool group on the shared FastMCP instance."""

    @mcp.tool()
    async def comment_task(
        task_id: Annotated[str, Field(min_length=1, description="Task id to comment on")],
        body: Annotated[str, Field(min_length=1, max_length=5000, description="Comment text")],
        status: Annotated[
            CommentStatus,
            Field(default="info", description="info | question | blocker | resolved"),
        ] = "info",
        parent_id: Annotated[
            int | None,
            Field(default=None, description="Reply to a top-level comment id (max depth 1)"),
        ] = None,
    ) -> dict[str, Any]:
        """Lascia un commento su un task (collaborazione cross-agente); notifica owner + partecipanti.

        QUANDO USARLO: fare una domanda/segnalare un blocco/dare un aggiornamento su un task che un altro agente o l'owner deve vedere; rispondere a un commento con parent_id.
        QUANDO NON USARLO: NOT se non hai grant sul progetto del task (→ 404). NOT per un log privato → usa un handoff/learning. Il corpo viene redatto (segreti rimossi) prima di salvare.
        RESTITUISCE: il commento creato {id, status, created_by, created_at, parent_id}."""
        ctx = current_mcp_context()
        try:
            async with acquire_write_db(label="mcp.comment_task") as db:
                result = await comments_uc.create_comment(
                    ctx,
                    db,
                    target_type="task",
                    target_id=task_id,
                    body=body,
                    status=status,
                    parent_id=parent_id,
                )
                return dump(result)
        except ServiceError as e:
            raise_mcp_error(e)

    @mcp.tool()
    async def list_comments(
        task_id: Annotated[str, Field(min_length=1, description="Task id whose comments to read")],
    ) -> list[dict[str, Any]]:
        """Leggi il thread di commenti di un task (con reazioni e risposte annidate).

        QUANDO USARLO: vedere la discussione su un task prima di agire o per rispondere a un commento di un altro agente.
        QUANDO NON USARLO: NOT se non hai grant sul progetto del task (→ 404, nessun leak dell'esistenza).
        RESTITUISCE: array di commenti top-level, ognuno con reactions[] e replies[] (threading a 1 livello)."""
        ctx = current_mcp_context()
        try:
            async with acquire_db() as db:
                result = await comments_uc.list_comments(
                    ctx, db, target_type="task", target_id=task_id
                )
                return dump(result)
        except ServiceError as e:
            raise_mcp_error(e)
