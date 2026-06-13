# v1.0.0 - 2026-05-27 - S1 F3.1a: search MCP tool group (use_cases-direct, no HTTP)
"""Search MCP tools — port of the Node ``search`` group, use_cases-direct.

Same template as ``tasks.py`` / ``learnings.py``: the Node HTTP proxy is replaced
by an in-process ``await search_uc.<action>(LOCAL_CTX, ...)``. Docstrings copied
VERBATIM from ``core/mcp-pir/index.mjs``.

Name mapping (Node tool name -> use_case function):
  * ``search``   -> ``search_uc.search`` (Node param ``q`` -> use_case param ``q``;
    the use_case opens its OWN connections function-locally from ``settings``, so
    NO ``acquire_db`` here — it takes only ``ctx``).
  * ``reindex``  -> ``search_uc.trigger_reindex`` (Node param ``type`` -> ``type``;
    operator+ role enforced inside the use_case, satisfied by the local operator
    ``LOCAL_CTX``).
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

from core.api.mcp._adapter import dump, raise_mcp_error, LOCAL_CTX
from core.api.use_cases import search as search_uc
from core.api.use_cases._errors import ServiceError

ReindexType = Literal[
    "tasks", "projects", "files", "handoffs", "learnings", "inbox_items", "audits", "all"
]


def register(mcp) -> None:
    """Register the search tool group on the shared FastMCP instance."""

    @mcp.tool()
    async def search(
        q: Annotated[str, Field(min_length=1, max_length=500)],
    ) -> dict[str, Any]:
        """Semantic + hybrid search by MEANING across tasks, projects, files, handoffs, learnings, inbox, audits. Fuses the embedding retriever with the Knowledge Graph (RRF).

        QUANDO USARLO: PRIMA scelta per domande trasversali o concettuali ('dove abbiamo parlato di caching Redis?', 'decisioni sul rollback', 'cosa dipende da X'): trova per significato anche senza le keyword esatte. Alza il tetto: usala invece di fermarti al primo summary o a una lista filtrata. Score > 0.6 generalmente rilevante.
        QUANDO NON USARLO: NOT per un artifact noto per ID -> usa get_task / get_handoff. NOT per un filtro esatto su un solo doc_type -> usa list_tasks / list_handoffs. NOT per keyword literal su un handoff -> usa search_handoffs.
        RESTITUISCE: {tasks[], projects[], files[], handoffs[], learnings[], total, query} ranked per (70% similarita semantica + 30% salience) + semantic_available (bool|None) + semantic_reason (enum|None: model-not-loadable/vec0-not-loadable/runtime-gate). Se semantic_available=false la lane semantica e' giu' e i risultati sono solo keyword. 503 se il motore di embedding non e' disponibile."""
        # The use_case takes only `ctx`: it opens its own connections from
        # settings (db_path / vec0_path) and pulls hybrid_search / embedding_service
        # function-locally — so the MCP path stays fastapi-free at import.
        try:
            result = await search_uc.search(LOCAL_CTX, q=q)
            return dump(result)
        except ServiceError as e:
            raise_mcp_error(e)

    @mcp.tool()
    async def reindex(type: ReindexType = "all") -> dict[str, Any]:
        """Rebuild the embeddings index for the semantic search backend.

        QUANDO USARLO: solo dopo bulk ops che bypassano i normali hook (direct SQL insert, file-sync script, migration) se noti search stale. Operator+.
        QUANDO NON USARLO: NOT routinely — l'index e' auto-sync su create/update normali. NOT su errori transient di search -> aspetta background reconcile.
        RESTITUISCE: type='all' -> {status:'queued'} background; tipo specifico -> sync result con counts."""
        # Operator+ role enforced inside the use_case; LOCAL_CTX is the local
        # operator. type='all' queues a background task; a specific type runs sync.
        try:
            result = await search_uc.trigger_reindex(LOCAL_CTX, type=type)
            return dump(result)
        except ServiceError as e:
            raise_mcp_error(e)
