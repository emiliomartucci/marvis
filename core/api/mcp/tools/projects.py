# v1.0.0 - 2026-05-27 - S1 F3.1a: projects MCP tool group (use_cases-direct, no HTTP)
"""Projects MCP tools — port of the Node ``projects`` group, use_cases-direct.

Same template as ``tasks.py`` / ``learnings.py``: the Node HTTP proxy
(``get(/api/v1/projects...)``) is replaced by an in-process
``await projects_uc.<action>(LOCAL_CTX, db, ...)``. Docstrings copied VERBATIM
from ``core/mcp-pir/index.mjs``.

Name mapping (Node tool name -> use_case function):
  * ``list_projects``  -> ``projects_uc.list_programs`` (the Node tool returns the
    project list grouped by program — that IS ``list_programs`` here).
  * ``get_project``    -> ``projects_uc.get_project``.
  * ``session_brief``  -> ``projects_uc.get_session_brief``.

Deep KG enrichment is DEFERRED (same as F3.0): ``get_project`` returns the core
fetch with ``kg_context=None`` and ignores ``deep`` (adapter concern, later F3
increment). ``session_brief``'s ``get_session_brief`` assembles kg_context inside
the use_case as part of the bundle contract — Node always cold-starts deep, so the
MCP surface passes ``deep=True`` to widen the per-bucket KG limits (Node
``effectiveDeep`` default).

Return typing: reads return ``dict[str, Any]`` / ``list[dict]`` via ``dump()``
(DTO lists are normalised element-wise). visible_projects=None (local single-user,
unrestricted — DECISION 1).
"""
from __future__ import annotations

from typing import Annotated, Any

from pydantic import Field

from core.api.mcp._adapter import LOCAL_CTX, acquire_db, dump, raise_mcp_error
from core.api.use_cases import projects as projects_uc
from core.api.use_cases._errors import ServiceError


def _lean_project_rows(
    programs: list[dict[str, Any]],
    *,
    lifecycle: str | None,
    program: str | None,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for group in programs:
        group_name = group.get("name")
        for project in group.get("projects", []) or []:
            project_program = project.get("program") or (
                None if group_name == "standalone" else group_name
            )
            if lifecycle is not None and project.get("lifecycle") != lifecycle:
                continue
            if program is not None and project_program != program:
                continue
            rows.append(
                {
                    "slug": project.get("slug"),
                    "program": project_program,
                    "lifecycle": project.get("lifecycle"),
                    "language": project.get("language"),
                    "task_counts": project.get("task_counts") or {},
                }
            )
    return rows


def _page_project_rows(
    rows: list[dict[str, Any]],
    *,
    limit: int,
    offset: int,
) -> list[dict[str, Any]]:
    return rows[offset : offset + limit]


def register(mcp) -> None:
    """Register the projects tool group on the shared FastMCP instance."""

    @mcp.tool()
    async def list_projects(
        lifecycle: Annotated[str, Field(max_length=50)] | None = None,
        program: Annotated[str, Field(max_length=100)] | None = None,
        limit: Annotated[int, Field(ge=1, le=200)] = 100,
        offset: Annotated[int, Field(ge=0)] = 0,
    ) -> list[dict[str, Any]]:
        """Lean paginated project inventory; use lifecycle/program filters server-side.

        QUANDO USARLO: progetti attivi -> list_projects(lifecycle='active'); inventario slug snello.
        QUANDO NON USARLO: slug noto + stato progetto -> session_brief; body context.md/docs -> get_project.
        PROVA: elenco slug/ciclo/contatori, non contesto completo.
        NEXT: session_brief(slug) sul progetto scelto.
        RESTITUISCE: list of {slug, program, lifecycle, language, task_counts}; default limit=100, max=200; mai context.md body."""
        try:
            async with acquire_db() as db:
                # Node `list_projects` returns the program-grouped project list =
                # `list_programs` here. visible_projects=None -> local sees all.
                result = await projects_uc.list_programs(
                    LOCAL_CTX, db, visible_projects=None
                )
                rows = _lean_project_rows(
                    dump(result), lifecycle=lifecycle, program=program
                )
                return _page_project_rows(rows, limit=limit, offset=offset)
        except ServiceError as e:
            raise_mcp_error(e)

    @mcp.tool()
    async def get_project(
        slug: Annotated[str, Field(min_length=1, max_length=50)],
        deep: bool | None = None,
    ) -> dict[str, Any]:
        """Full raw detail for one known project: metadata, context.md, handoffs and docs.

        QUANDO USARLO: hai lo slug e ti serve context.md body o indice docs/handoff.
        QUANDO NON USARLO: cold-start agent -> session_brief; inventario -> list_projects.
        PROVA: body raw del progetto noto; non sostituisce la bundle cold-start.
        RESTITUISCE: {slug, metadata, context_md, handoffs[], docs[], deploy_info}."""
        try:
            async with acquire_db() as db:
                # DECISION 2: `deep` KG enrichment is an adapter concern; the
                # use_case returns kg_context=None (core fetch). The deep-attach
                # is a later F3 increment, same as get_task/get_learning in F3.0.
                result = await projects_uc.get_project(
                    LOCAL_CTX, db, slug=slug, visible_projects=None
                )
                return dump(result)
        except ServiceError as e:
            raise_mcp_error(e)

    @mcp.tool()
    async def session_brief(
        slug: Annotated[str, Field(min_length=1, max_length=50)],
    ) -> dict[str, Any]:
        """Cold-start bundle for one project.

        QUANDO USARLO: prima call su uno slug; sostituisce get_project + list_tasks + handoff/learnings. CANONICALITY: usa repo_path/metadata_path ritornati come fonte hosted, non path locali.
        QUANDO NON USARLO: body context.md/docs completi -> get_project; filtri puntuali -> list_tasks/list_handoffs.
        PROVA: stato progetto, task aperti, latest_handoff, learnings e repo_path hosted.
        NEXT: check_learnings prima di codice/deploy/reindex.
        RESTITUISCE: {project, open_tasks[], latest_handoff, recent_learnings[], top_salience_docs[]}."""
        try:
            async with acquire_db() as db:
                # The bundle's kg_context is part of the contract (assembled inside
                # the use_case, not deferred). Node cold-starts deep by default
                # (`effectiveDeep` -> true), so widen the per-bucket KG limits.
                result = await projects_uc.get_session_brief(
                    LOCAL_CTX, db, slug=slug, deep=True, visible_projects=None
                )
                return dump(result)
        except ServiceError as e:
            raise_mcp_error(e)
