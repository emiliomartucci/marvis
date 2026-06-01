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


def register(mcp) -> None:
    """Register the projects tool group on the shared FastMCP instance."""

    @mcp.tool()
    async def list_projects() -> list[dict[str, Any]]:
        """List all PiR-tracked projects grouped by program.

        QUANDO USARLO: serve un inventario (quali progetti ci sono, dammi tutti gli slug) o non conosci lo slug target.
        QUANDO NON USARLO: NOT quando hai gia' uno slug specifico e vuoi i dettagli -> usa get_project. NOT per cold-start di un progetto -> usa session_brief.
        RESTITUISCE: list of {slug, program, lifecycle, language, task_counts} senza body di context.md."""
        try:
            async with acquire_db() as db:
                # Node `list_projects` returns the program-grouped project list =
                # `list_programs` here. visible_projects=None -> local sees all.
                result = await projects_uc.list_programs(
                    LOCAL_CTX, db, visible_projects=None
                )
                return dump(result)
        except ServiceError as e:
            raise_mcp_error(e)

    @mcp.tool()
    async def get_project(
        slug: Annotated[str, Field(min_length=1, max_length=50)],
        deep: bool | None = None,
    ) -> dict[str, Any]:
        """Get full project detail (metadata + context.md body + handoff index + docs).

        QUANDO USARLO: hai uno slug noto e ti serve lo stato raw del progetto (es. leggere context.md body o elenco handoff completo). Usa ?deep=true per includere kg_context inline (neighbors, context_chain, applicable_learnings) — risparmia 2-3 tool call aggiuntivi.
        QUANDO NON USARLO: NOT per agent cold-start -> usa session_brief (aggrega project + open tasks + top learnings + salience docs in una call). NOT se non hai lo slug -> usa list_projects.
        RESTITUISCE: {slug, metadata, context_md, handoffs[], docs[], deploy_info} + kg_context se deep=true."""
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
        """Cold-start aggregated context bundle for agent session resume.

        QUANDO USARLO: inizio sessione su un progetto (sostituisce la sequenza get_project + list_tasks + search_handoffs + Read context.md + check_learnings). BOUNDARY: session_brief = cold-start aggregato; list_tasks+list_handoffs = query mirate.
        QUANDO NON USARLO: NOT se ti serve solo il body grezzo di context.md o l'elenco completo handoff -> usa get_project. NOT per query filtrate puntuali -> usa list_tasks/list_handoffs.
        RESTITUISCE: {project, open_tasks[], latest_handoff, recent_learnings[], top_salience_docs[]} tuned per context window LLM."""
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
