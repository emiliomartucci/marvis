# v1.0.0 - 2026-05-27 - S1 F3.1a: search MCP tool group (use_cases-direct, no HTTP)
"""Search MCP tools — port of the Node ``search`` group, use_cases-direct.

Same template as ``tasks.py`` / ``learnings.py``: the Node HTTP proxy is replaced
by an in-process ``await search_uc.<action>(current_mcp_context(), ...)``. Docstrings copied
VERBATIM from ``core/mcp-pir/index.mjs``.

Name mapping (Node tool name -> use_case function):
  * ``search``   -> ``search_uc.search`` (Node param ``q`` -> use_case param ``q``;
    the use_case opens its OWN connections function-locally from ``settings``, so
    NO ``acquire_db`` here — it takes only ``ctx``).
  * ``reindex``  -> ``search_uc.trigger_reindex`` (Node param ``type`` -> ``type``;
    operator+ role enforced inside the use_case; broad remote scans fail closed
    because the current use case cannot accept a project allowlist).
  * ``reindex_paths`` -> ``search_uc.reindex_file_paths`` (delta-only file reindex
    for explicit project paths; avoids the broad ``reindex(type='files')`` scan).
  * ``boost_document`` -> **SKIPPED**. No use_case exists yet — boost lives only in
    ``core/api/routers/documents.py`` (a fastapi router not extracted to a
    use_case). Importing the router would drag fastapi into the collapsed MCP
    runtime (violates the no-fastapi-in-MCP invariant, S1 §AUTH / tasks.py seam
    note), so it is deferred to the batch that extracts ``documents`` to a
    use_case. Reported in the task body.

The embedding/hybrid services are pulled FUNCTION-LOCALLY by the use_case itself
(``hybrid_search`` / ``embedding_service``), so nothing fastapi-bound is imported
at module load. ``search`` raises ``ServiceUnavailableError`` (-> raise_mcp_error)
when the embedding backend is down — the 503 collapses to the MCP error result shape.

Return typing: ``search`` returns the ``SearchResponse`` DTO (``.model_dump()`` via
``dump()``); ``reindex`` returns a plain status dict.
"""
from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import Field

from core.api.mcp._adapter import (
    LOCAL_CTX,
    acquire_db,
    current_mcp_context,
    current_visible_projects,
    dump,
    no_grants_notice,
    require_unambiguous_visible_project,
    raise_mcp_error,
)
from core.api.use_cases import search as search_uc
from core.api.use_cases._errors import AuthorizationError, NotFoundError, ServiceError

ReindexType = Literal[
    "tasks", "projects", "files", "handoffs", "learnings", "inbox_items", "audits", "all"
]
ReindexPath = Annotated[str, Field(min_length=1, max_length=2000)]
ReindexPathList = Annotated[list[ReindexPath], Field(min_length=1, max_length=100)]


def _trusted_local_stdio(ctx) -> bool:
    """Identity-safe transport distinction: stdio receives the singleton itself."""
    return ctx is LOCAL_CTX


async def _remote_reindex_visible_projects(db, ctx) -> set[str]:
    """Return only grants whose disk-backed slug has one workspace owner."""
    visible_projects = await current_visible_projects(db, ctx)
    if visible_projects is None:
        return set()
    safe: set[str] = set()
    for project_slug in sorted(visible_projects):
        try:
            await require_unambiguous_visible_project(
                db,
                ctx,
                project_slug,
                visible_projects,
            )
        except NotFoundError:
            continue
        safe.add(project_slug)
    return safe


def register(mcp) -> None:
    """Register the search tool group on the shared FastMCP instance."""

    @mcp.tool()
    async def search(
        q: Annotated[str, Field(min_length=1, max_length=500)],
    ) -> dict[str, Any]:
        """MEANING-first discovery across tasks, projects, files, handoffs, learnings, inbox and audits.

        QUANDO USARLO: discovery per significato, cross-project o concettuale quando non sai l'artefatto esatto.
        QUANDO NON USARLO: ID/slug noto -> get_task/get_project/get_handoff; filtri esatti -> list_tasks/list_handoffs.
        PROVA: buckets ranked con span evidence, non stato completo.
        NEXT: apri l'artefatto esatto con get_*/read_file dopo aver scelto il match.
        RESTITUISCE: buckets ranked (tasks/projects/files/handoffs/learnings/inbox_items/audits), ogni hit con doc_id (usato da memory_feedback) + span evidence; usa span_text prima di aprire file. Con reinforcement mode shadow/on e risultati non vuoti, il payload include il campo suggested_next_tool (nudge memory_feedback)."""
        # The use_case takes only `ctx`: it opens its own connections from
        # settings (db_path / vec0_path) and pulls hybrid_search / embedding_service
        # function-locally — so the MCP path stays fastapi-free at import.
        try:
            ctx = current_mcp_context()
            async with acquire_db() as db:
                visible_projects = await current_visible_projects(db, ctx)
            result = await search_uc.search(ctx, q=q)
            payload = dump(result)
            notice = no_grants_notice(visible_projects)
            if notice:
                payload.update(notice)
            return payload
        except ServiceError as e:
            raise_mcp_error(e)

    @mcp.tool()
    async def reindex(type: ReindexType = "all") -> dict[str, Any]:
        """Rebuild the embeddings index for the semantic search backend.

        QUANDO USARLO: solo dopo bulk ops che bypassano i normali hook (direct SQL insert, file-sync script, migration) se noti search stale. Operator+.
        QUANDO NON USARLO: NOT routinely — l'index e' auto-sync su create/update normali. NOT su errori transient di search -> aspetta background reconcile.
        RESTITUISCE: type='all' -> {status:'queued'} background; tipo specifico -> sync result con counts."""
        # Broad scans cannot currently receive a project allowlist. Keep the
        # trusted stdio behavior, but fail closed on every remote transport.
        try:
            ctx = current_mcp_context()
            if not _trusted_local_stdio(ctx):
                raise AuthorizationError(
                    code="remote_reindex_scope_unsupported",
                    message=(
                        "Remote broad reindex cannot be scoped to the caller's "
                        "visible projects; use reindex_paths with explicit paths"
                    ),
                )
            result = await search_uc.trigger_reindex(ctx, type=type)
            return dump(result)
        except ServiceError as e:
            raise_mcp_error(e)

    @mcp.tool()
    async def reindex_paths(paths: ReindexPathList) -> dict[str, Any]:
        """Rebuild semantic search only for explicit project file paths.

        QUANDO USARLO: dopo sync/migration/copy mirata di file markdown sotto projects_root, quando vuoi refresh search senza scansione globale.
        QUANDO NON USARLO: NOT per tutti i file -> usa reindex(type='files') solo se accetti scan largo. NOT per KG nodes/edges -> usa kg_reindex_path.
        RESTITUISCE: {status:'ok', type:'files', indexed, skipped, total, skipped_entries?}."""
        try:
            ctx = current_mcp_context()
            visible_projects = None
            if not _trusted_local_stdio(ctx):
                async with acquire_db() as db:
                    visible_projects = await _remote_reindex_visible_projects(db, ctx)
            result = await search_uc.reindex_file_paths(
                ctx,
                paths=paths,
                visible_projects=visible_projects,
            )
            return dump(result)
        except ServiceError as e:
            raise_mcp_error(e)
