# v1.1.0 - 2026-09-22 - Plan SP-E: gather_context kind=task and kind=spot.
"""Context MCP tools — gather_context, the Jev-aware cold-start primitive.

Plan KTD5 + SP-E: ``gather_context(slug, goal, kind)`` composes on the SAME
session_brief path; the goal-conditioned selection is ONE provider batch.
kind=session is the full brief; kind=task adds task-anchored context;
kind=spot is a quick lookup for a specific question.
"""
from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import Field

from core.api.mcp._adapter import (
    acquire_db,
    attach_notices,
    current_mcp_context,
    current_visible_projects,
    dump,
    raise_mcp_error,
    require_any_grant,
    require_unambiguous_visible_project,
)
from core.api.use_cases import projects as projects_uc
from core.api.use_cases._errors import NotFoundError, ServiceError, ValidationError

GatherKind = Literal["session", "task", "spot"]


async def _task_bundle(
    db: Any, ctx: Any, slug: str, task_id: str, visible_projects: Any,
) -> dict[str, Any]:
    """Bundle for kind=task: the task + its context (learnings, graph)."""
    from core.api.use_cases import tasks as tasks_uc
    from core.api.use_cases.learnings import check_learnings as _cl
    task = await tasks_uc.get_task(ctx, db, task_id=task_id, visible_projects=visible_projects)
    task_dict = task.model_dump() if hasattr(task, "model_dump") else dict(task)
    if task_dict.get("project") != slug:
        raise NotFoundError(code="task_not_found", message="Task not found")

    # Relevant learnings via graph pattern on the task's tags/description
    task_text = f"{task_dict.get('title', '')} {task_dict.get('description', '')}"
    learnings = await _cl(ctx, db, query=task_text[:500], visible_projects=visible_projects)
    learning_dicts = [
        {"id": l.id, "title": l.title, "description": l.description,
         "prevention": l.prevention, "project": l.project}
        for l in learnings.results if l.project in (None, slug)
    ][:6]

    return {
        "project": {"slug": slug},
        "open_tasks": [task_dict],
        "recent_learnings": learning_dicts,
        "top_salience_docs": [],
        "latest_handoff": None,
        "_task_anchor": task_dict.get("title", ""),
        "_task_anchor_id": task_id,
    }


async def _spot_bundle(
    db: Any, ctx: Any, slug: str, question: str, visible_projects: Any,
) -> dict[str, Any]:
    """Bundle for kind=spot: targeted search results + relevant learnings."""
    from core.api.use_cases import search as search_uc
    resp = await search_uc.search(
        ctx, q=question, limit=10, project=slug, allow_rerank=False,
    )
    spot = resp.model_dump() if hasattr(resp, "model_dump") else dump(resp)

    def hits(bucket: str, limit: int) -> list[dict[str, Any]]:
        return [
            {**hit, "id": hit.get("doc_id", ""), "description": hit.get("span_text") or ""}
            for hit in spot.get(bucket, []) if hit.get("project") == slug
        ][:limit]

    handoffs = hits("handoffs", 1)
    return {
        "project": {"slug": slug},
        "open_tasks": hits("tasks", 4),
        "recent_learnings": hits("learnings", 4),
        "top_salience_docs": hits("files", 3),
        "latest_handoff": handoffs[0] if handoffs else None,
        "semantic_available": spot.get("semantic_available"),
        "semantic_reason": spot.get("semantic_reason"),
        "_spot_question": question[:200],
    }


def register(mcp) -> None:  # noqa: ANN001 — FastMCP decorator API
    @mcp.tool()
    async def gather_context(
        slug: Annotated[str, Field(min_length=1, max_length=50)],
        goal: Annotated[
            str,
            Field(
                max_length=500,
                description=(
                    "What you are about to work on (session), the task to "
                    "prepare (task), or the question to answer (spot). "
                    "Empty goal = the plain session_brief bundle."
                ),
            ),
        ] = "",
        kind: GatherKind = "session",
        task_id: Annotated[
            str,
            Field(
                max_length=64,
                description="Required when kind='task': the task UUID to prepare context for.",
            ),
        ] = "",
    ) -> dict[str, Any]:
        """Goal-conditioned working context for one project (Jev-aware session_brief).

        QUANDO USARLO: prima call su uno slug quando hai gia' un goal; con un goal, il bundle viene curato: gli item fuori-goal vengono omessi CON ricevuta (blocco `curation`). kind=session = brief completo; kind=task = contesto per lavorare su un task specifico (task + learnings correlate); kind=spot = risposta veloce a una domanda puntuale.
        QUANDO NON USARLO: cold-start senza goal -> session_brief (alias legacy, identico a oggi).
        PROVA: bundle curato + curation receipt (status, omitted, suggested_tools).
        NEXT: i suggested_tools del blocco curation sono un consiglio, non un vincolo: decidi tu.
        RESTITUISCE: bundle (+ curation quando c'e' un goal). kind=task richiede task_id. Fail-closed: provider giu' -> bundle intatto."""
        try:
            async with acquire_db() as db:
                ctx = current_mcp_context()
                visible_projects = await current_visible_projects(db, ctx)
                require_any_grant(visible_projects)
                if kind != "session":
                    await require_unambiguous_visible_project(db, ctx, slug, visible_projects)

                if kind == "task" and not task_id.strip():
                    raise ValidationError(
                        code="missing_task_id",
                        message="kind='task' requires the task_id parameter.",
                    )
                if kind == "session":
                    result = await projects_uc.get_session_brief(
                        ctx, db, slug=slug, deep=True, visible_projects=visible_projects
                    )
                    bundle = dump(result)
                elif kind == "task":
                    bundle = await _task_bundle(db, ctx, slug, task_id.strip(), visible_projects)
                    if not goal.strip():
                        task = bundle["open_tasks"][0]
                        goal = f"{task['title']} {task.get('description') or ''}"[:500]
                elif kind == "spot":
                    if not goal.strip():
                        raise ValidationError(
                            code="missing_goal",
                            message="kind='spot' requires a goal (the question).",
                        )
                    bundle = await _spot_bundle(db, ctx, slug, goal.strip(), visible_projects)
                else:
                    raise ValidationError(
                        code="unsupported_kind",
                        message=f"kind='{kind}' is not supported; use session, task or spot.",
                    )

                # Selection AFTER the deterministic bundle exists: one batch,
                # omission-only-with-receipt, fail-closed (plan KTD5).
                from core.api.services.jev.gather_context import select_bundle_items

                bundle, curation = await select_bundle_items(bundle, goal)
                bundle["curation"] = curation.to_dict()
                return await attach_notices(
                    db, bundle, ctx, visible_projects=visible_projects, project=slug
                )
        except ServiceError as e:
            raise_mcp_error(e)
