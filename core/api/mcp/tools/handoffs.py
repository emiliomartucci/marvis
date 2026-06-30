# v1.0.0 - 2026-05-27 - S1 F3.1a: handoffs MCP tool group (use_cases-direct, no HTTP)
"""Handoffs MCP tools — port of the Node ``handoffs`` group, use_cases-direct.

Same template as ``tasks.py`` / ``learnings.py``: the Node HTTP proxy is replaced
by an in-process ``await <uc>.<action>(LOCAL_CTX, db, ...)``. Docstrings copied
VERBATIM from ``core/mcp-pir/index.mjs``.

Name mapping (Node tool name -> use_case function):
  * ``list_handoffs``   -> ``projects_uc.get_handoffs`` (lives in the projects
    use_case — handoff listing is a project-index read, mirroring the Node route
    ``/api/v1/projects/<slug>/handoffs``).
  * ``search_handoffs`` -> ``handoffs_uc.search_handoffs`` (needs BOTH a read ``db``
    and a ``vec_db``; the HTTP router injects ``vec_db`` via ``Depends(get_vec_db)``,
    so the MCP surface wraps the same ``get_vec_db`` generator as a context manager
    FUNCTION-LOCALLY — no fastapi, just the sqlite-vec connection).
  * ``get_handoff``     -> ``handoffs_uc.get_handoff`` (read ``db`` only).

Deep KG enrichment is DEFERRED (same as F3.0): ``get_handoff`` returns the core
fetch with ``kg_context=None`` and ignores ``deep`` (adapter concern, later F3
increment).

Return typing: reads return ``dict[str, Any]`` / ``list[dict]`` via ``dump()``
(DTO lists normalised element-wise). visible_projects=None (local single-user,
unrestricted — DECISION 1).
"""
from __future__ import annotations

from typing import Annotated, Any

from pydantic import Field

from core.api.mcp._adapter import (
    LOCAL_CTX,
    acquire_db,
    acquire_write_db,
    dump,
    raise_mcp_error,
)
from core.api.use_cases import handoffs as handoffs_uc
from core.api.use_cases import projects as projects_uc
from core.api.use_cases._errors import ServiceError


def _normalize_list_handoff_entry(slug: str, entry: dict[str, Any]) -> dict[str, Any]:
    """Make list_handoffs rows directly usable by get_handoff."""
    raw_filename = str(entry.get("filename") or "")
    filename = raw_filename.rsplit("/", 1)[-1] if raw_filename else raw_filename
    normalized = dict(entry)
    normalized["path"] = raw_filename
    normalized["filename"] = filename
    normalized["project_slug"] = slug
    normalized["project"] = slug
    if "session" in normalized and "session_id" not in normalized:
        normalized["session_id"] = normalized["session"]
    return normalized


def _normalize_search_handoff_result(entry: dict[str, Any]) -> dict[str, Any]:
    """Make search_handoffs rows directly usable by get_handoff."""
    raw_file = str(entry.get("file") or entry.get("filename") or "")
    filename = raw_file.rsplit("/", 1)[-1] if raw_file else raw_file
    project = str(entry.get("project") or entry.get("project_slug") or "")
    normalized = dict(entry)
    normalized["file"] = filename
    normalized["filename"] = filename
    normalized["project"] = project
    normalized["project_slug"] = project
    normalized.setdefault("path", f"memory/{filename}" if filename else raw_file)
    return normalized


def _page_handoff_rows(
    rows: list[dict[str, Any]], *, limit: int, offset: int
) -> list[dict[str, Any]]:
    """Keep list_handoffs useful as a list view instead of a full history dump."""
    return rows[offset:offset + limit]


def _invalid_handoff_filename_error() -> ServiceError:
    return ServiceError(
        code="invalid_filename",
        message="Invalid filename — must match handoff-*.md or memory/handoff-*.md",
    )


def _normalize_get_handoff_filename(filename: str) -> str:
    """Accept the path emitted by list_handoffs without widening file access."""
    if filename.startswith("memory/"):
        filename = filename.removeprefix("memory/")
    elif "/" in filename or "\\" in filename:
        raise _invalid_handoff_filename_error()

    if "/" in filename or "\\" in filename:
        raise _invalid_handoff_filename_error()
    return filename


def register(mcp) -> None:
    """Register the handoffs tool group on the shared FastMCP instance."""

    @mcp.tool()
    async def create_handoff(
        project_slug: Annotated[str, Field(min_length=1, max_length=63)],
        body: Annotated[str, Field(min_length=1, max_length=100_000)],
        title: Annotated[str | None, Field(max_length=200)] = None,
        summary: Annotated[str | None, Field(max_length=2_000)] = None,
        filename: Annotated[str | None, Field(min_length=1, max_length=200)] = None,
        date: Annotated[str | None, Field(pattern=r"^\d{4}-\d{2}-\d{2}$")] = None,
        session: Annotated[str | None, Field(max_length=80)] = None,
        branch: Annotated[str | None, Field(max_length=200)] = None,
        tags: Annotated[list[str] | None, Field(max_length=30)] = None,
        task_id: Annotated[str | None, Field(max_length=80)] = None,
    ) -> dict[str, Any]:
        """Create a project handoff file through MCP.

        QUANDO USARLO: chiudere una sessione o passare contesto tra agenti senza
        Console/filesystem manuale. Passa markdown body senza YAML frontmatter:
        il tool crea frontmatter canonico e un file memory/handoff-*.md.
        QUANDO NON USARLO: NOT per leggere handoff esistenti -> usa
        list_handoffs/get_handoff. NOT per cercare testo -> usa search_handoffs.
        RESTITUISCE: {project_slug, filename, path, frontmatter, body};
        project_slug+filename feed get_handoff directly."""
        try:
            async with acquire_write_db() as db:
                result = await handoffs_uc.create_handoff(
                    LOCAL_CTX,
                    db,
                    project_slug=project_slug,
                    body=body,
                    title=title,
                    summary=summary,
                    filename=filename,
                    date=date,
                    session=session,
                    branch=branch,
                    tags=tags,
                    task_id=task_id,
                    visible_projects=None,
                )
                return dump(result)
        except ServiceError as e:
            raise_mcp_error(e)

    @mcp.tool()
    async def list_handoffs(
        slug: Annotated[str, Field(min_length=1, max_length=50)],
        limit: Annotated[int, Field(ge=1, le=200)] = 20,
        offset: Annotated[int, Field(ge=0)] = 0,
    ) -> list[dict[str, Any]]:
        """List handoff files for one project (frontmatter metadata only, no body).

        QUANDO USARLO: browse chronological session history per progetto noto (es. quali sessioni su marvisx ultima settimana). BOUNDARY: session_brief vs list_tasks+list_handoffs = session_brief aggrega tutto in una call.
        QUANDO NON USARLO: NOT per keyword search cross-project -> usa search_handoffs. NOT per cold-start -> usa session_brief.
        RESTITUISCE: paginated list of {project_slug, filename, path, session_id, branch, tags, date} ordered chronological; project_slug+filename feed get_handoff directly."""
        try:
            async with acquire_db() as db:
                # Node route is /projects/<slug>/handoffs -> projects_uc.get_handoffs.
                result = await projects_uc.get_handoffs(
                    LOCAL_CTX, db, slug=slug, visible_projects=None
                )
                rows = dump(result)
                normalized_rows = [
                    _normalize_list_handoff_entry(slug, row) for row in rows
                ]
                return _page_handoff_rows(
                    normalized_rows, limit=limit, offset=offset
                )
        except ServiceError as e:
            raise_mcp_error(e)

    @mcp.tool()
    async def search_handoffs(
        q: Annotated[str, Field(min_length=1, max_length=500)],
    ) -> list[dict[str, Any]]:
        """Full-text keyword search across every handoff body in every project.

        QUANDO USARLO: ricordi un termine ma non il progetto/data (es. 'find handoffs mentioning migration 025'). Exact keyword match, NOT semantic.
        QUANDO NON USARLO: NOT per semantic discovery multi-type -> usa search. NOT se conosci il progetto -> usa list_handoffs.
        RESTITUISCE: list of {project_slug, filename, project, file, snippet, date} con match literal; project_slug+filename feed get_handoff directly."""
        # search_handoffs needs both a read `db` and a `vec_db`. The HTTP router
        # injects vec_db via Depends(get_vec_db); on the MCP surface we wrap the
        # SAME get_vec_db async-generator as a context manager FUNCTION-LOCALLY
        # (fastapi-free: get_vec_db is a plain aiosqlite connection helper).
        from contextlib import asynccontextmanager

        from core.api.db import get_vec_db

        acquire_vec_db = asynccontextmanager(get_vec_db)
        try:
            async with acquire_db() as db, acquire_vec_db() as vec_db:
                result = await handoffs_uc.search_handoffs(
                    LOCAL_CTX, db, vec_db, q=q, visible_projects=None
                )
                rows = dump(result)
                return [_normalize_search_handoff_result(row) for row in rows]
        except ServiceError as e:
            raise_mcp_error(e)

    @mcp.tool()
    async def get_handoff(
        project_slug: Annotated[str, Field(min_length=1, max_length=63)],
        filename: Annotated[str, Field(min_length=1, max_length=200)],
        deep: bool | None = None,
    ) -> dict[str, Any]:
        """Get a single handoff file by project and filename.

        QUANDO USARLO: hai project_slug + filename da list_handoffs/search_handoffs e vuoi il body completo + frontmatter. Usa ?deep=true per kg_context inline (references, mentions, context chain).
        QUANDO NON USARLO: NOT per ricerca testuale -> usa search_handoffs. NOT per lista handoff di progetto -> usa list_handoffs.
        RESTITUISCE: {project, file, frontmatter, body, kg_context?}."""
        try:
            normalized_filename = _normalize_get_handoff_filename(filename)
            async with acquire_db() as db:
                # DECISION 2: `deep` KG enrichment is an adapter concern; the
                # use_case returns kg_context=None (core fetch). Deep-attach lands
                # in a later F3 increment, same as get_task/get_project in F3.0.
                result = await handoffs_uc.get_handoff(
                    LOCAL_CTX,
                    db,
                    project_slug=project_slug,
                    filename=normalized_filename,
                    visible_projects=None,
                )
                return dump(result)
        except ServiceError as e:
            raise_mcp_error(e)
