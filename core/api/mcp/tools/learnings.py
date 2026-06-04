# v1.0.0 - 2026-05-27 - S1 F3.0: learnings MCP tool group (use_cases-direct, no HTTP)
"""Learnings MCP tools — port of the Node ``learnings`` group, use_cases-direct.

Same template as ``tasks.py``: the Node HTTP proxy is replaced by an in-process
``await learnings_uc.<action>(LOCAL_CTX, db, ...)``. Docstrings copied
VERBATIM from ``core/mcp-pir/index.mjs``.

Schema notes:
  * ``check_learnings`` keeps the Node param name ``q`` (the surface contract);
    the use_case parameter is ``query`` — mapped in the body. ``module`` optional.
  * ``create_learning`` mirrors the Node REQUIRED set (title/category/description/
    prevention/severity) and the optional set (module/tags/project). Enums become
    ``Literal[...]``.

Return typing: reads return ``dict[str, Any]`` / ``list[dict]``; ``create_learning``
(mutator) returns the DTO ``.model_dump()`` via ``dump()``. visible_projects=None
(local single-user, unrestricted).
"""
from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import Field

from core.api.mcp._adapter import (
    LOCAL_CTX,
    acquire_db,
    acquire_write_db,
    dump,
    mcp_embed_learning,
    raise_mcp_error,
)
from core.api.use_cases import learnings as learnings_uc
from core.api.use_cases._errors import ServiceError

LearningCategory = Literal[
    "deploy", "migration", "auth", "testing", "architecture", "security", "performance"
]
LearningSeverity = Literal["low", "medium", "high", "critical"]


def register(mcp) -> None:
    """Register the learnings tool group on the shared FastMCP instance."""

    @mcp.tool()
    async def check_learnings(
        q: Annotated[str, Field(min_length=1, max_length=500)],
        module: str | None = None,
    ) -> dict[str, Any]:
        """Search learnings DB (past post-mortems + prevention rules) for a risky action.

        QUANDO USARLO: PRIMA di qualunque azione production-affecting (deploy, migration, auth change, push to main, dependency upgrade) — Quality Gate 9.4. BOUNDARY: create_learning vs check_learnings = create scrive nuovo; check cerca rilevante PRE-azione rischiosa. BOUNDARY: check_learnings vs list_learnings = check = semantic match su situazione; list = enumerazione filtrata.
        QUANDO NON USARLO: NOT quando hai gia' un file/module path e vuoi learnings scoped -> usa graph_pattern. NOT per scrivere un nuovo learning -> usa create_learning.
        RESTITUISCE: list of {id, title, prevention, severity, module} ranked per rilevanza."""
        try:
            async with acquire_db() as db:
                # Node param `q` -> use_case param `query`.
                result = await learnings_uc.check_learnings(
                    LOCAL_CTX, db, query=q, module=module
                )
                return dump(result)
        except ServiceError as e:
            raise_mcp_error(e)

    @mcp.tool()
    async def create_learning(
        title: Annotated[str, Field(min_length=1, max_length=200)],
        category: LearningCategory,
        description: Annotated[str, Field(min_length=1)],
        prevention: Annotated[str, Field(min_length=1)],
        severity: LearningSeverity,
        module: str | None = None,
        tags: list[str] | None = None,
        project: str | None = None,
    ) -> dict[str, Any]:
        """Persist a new learning (post-mortem) with prevention rule after an error or incident.

        QUANDO USARLO: DOPO aver hit un bug che vale la pena ricordare cross-session (es. 'z.record(1-arg) crashes MCP SDK'). BOUNDARY: create_learning vs check_learnings = create scrive nuovo; check cerca rilevante PRE-azione rischiosa.
        QUANDO NON USARLO: NOT per idea forward-looking improvement -> usa create_task kind='idea'. NOT per task operativo -> usa create_task.
        RESTITUISCE: {id, title, category, severity, created_at} — indexed by check_learnings."""
        try:
            async with acquire_write_db(label="mcp.create_learning") as db:
                result = await learnings_uc.create_learning(
                    LOCAL_CTX,
                    db,
                    title=title,
                    category=category,
                    description=description,
                    prevention=prevention,
                    severity=severity,
                    module=module,
                    tags=tags,
                    project=project,
                )
            # Writer lock released: embed-on-write. On a local Granite backend this
            # awaits inline (synchronous) so the learning is immediately retrievable by
            # meaning — not just by keyword until a manual reindex (the bug this fixes).
            await mcp_embed_learning(
                learning_id=result.id,
                title=result.title,
                description=result.description,
                category=result.category,
                severity=result.severity,
                prevention=result.prevention,
                project=result.project,
                workspace_id=LOCAL_CTX.workspace_id,
            )
            return dump(result)
        except ServiceError as e:
            raise_mcp_error(e)

    @mcp.tool()
    async def update_learning(
        learning_id: Annotated[str, Field(min_length=1)],
        title: Annotated[str, Field(min_length=1, max_length=200)] | None = None,
        category: LearningCategory | None = None,
        description: Annotated[str, Field(min_length=1)] | None = None,
        prevention: Annotated[str, Field(min_length=1)] | None = None,
        severity: LearningSeverity | None = None,
        module: str | None = None,
        tags: list[str] | None = None,
        project: str | None = None,
    ) -> dict[str, Any]:
        """Update an existing learning (operator+), then re-embed it so search/check_learnings see the new content immediately.

        QUANDO USARLO: il contenuto di un learning e' obsoleto/incompleto (prevention piu' precisa, severity ricalibrata, nuovo modulo) ma vuoi PRESERVARE la history. BOUNDARY: update_learning aggiorna in-place + re-embed; delete_learning cancella permanente; create_learning scrive nuovo.
        QUANDO NON USARLO: NOT per un learning nuovo -> usa create_learning. NOT per azzerare un campo a null: la superficie MCP NON puo' settare null (omettere un campo lo lascia invariato) -> usa l'HTTP PATCH se devi pulire un campo.
        RESTITUISCE: il learning aggiornato {id, title, category, severity, prevention, tags, module, project, updated_at} — gia' ri-embeddato (retrievable by meaning, non solo keyword)."""
        fields = {
            k: v
            for k, v in {
                "title": title,
                "category": category,
                "description": description,
                "prevention": prevention,
                "severity": severity,
                "module": module,
                "tags": tags,
                "project": project,
            }.items()
            if v is not None
        }
        try:
            async with acquire_write_db(label="mcp.update_learning") as db:
                result = await learnings_uc.update_learning(
                    LOCAL_CTX, db, learning_id=learning_id, fields=fields
                )
            # Writer lock released: re-embed-on-write (mirror create_learning) so the
            # updated content is retrievable by meaning immediately, not stale until
            # a manual reindex.
            await mcp_embed_learning(
                learning_id=result.id,
                title=result.title,
                description=result.description,
                category=result.category,
                severity=result.severity,
                prevention=result.prevention,
                project=result.project,
                workspace_id=LOCAL_CTX.workspace_id,
            )
            return dump(result)
        except ServiceError as e:
            raise_mcp_error(e)

    @mcp.tool()
    async def list_learnings(
        project: str | None = None,
        category: LearningCategory | None = None,
        severity: LearningSeverity | None = None,
        tags: str | None = None,
        search: str | None = None,
        limit: Annotated[int, Field(ge=1, le=200)] = 50,
        offset: Annotated[int, Field(ge=0)] = 0,
    ) -> list[dict[str, Any]]:
        """List learnings with optional filters (category, project, severity, tags, free text).

        QUANDO USARLO: enumerare tutti i learning del progetto o filtrare per category/severity/tags. Utile per review periodica pattern consolidati. BOUNDARY: check_learnings vs list_learnings = check = semantic match su situazione; list = enumerazione filtrata.
        QUANDO NON USARLO: NOT per cercare learning applicabile a situazione rischiosa corrente -> usa check_learnings (semantic). NOT per singolo ID -> usa get_learning.
        RESTITUISCE: list of {id, title, category, severity, module, project, created_at, tags} paginato ordered per frequency DESC."""
        try:
            async with acquire_db() as db:
                result = await learnings_uc.list_learnings(
                    LOCAL_CTX,
                    db,
                    category=category,
                    project=project,
                    severity=severity,
                    tags=tags,
                    search=search,
                    limit=limit,
                    offset=offset,
                    visible_projects=None,
                )
                return dump(result)
        except ServiceError as e:
            raise_mcp_error(e)

    @mcp.tool()
    async def get_learning(
        learning_id: Annotated[str, Field(min_length=1)],
        deep: bool | None = None,
    ) -> dict[str, Any]:
        """Get a single learning by UUID.

        QUANDO USARLO: hai un learning ID (da list_learnings o check_learnings) e vuoi il body completo (description + prevention + tags). Usa ?deep=true per includere kg_context inline (context chain, related learnings) — risparmia 2-3 tool call aggiuntivi.
        QUANDO NON USARLO: NOT per ricerca semantica pre-azione -> usa check_learnings. NOT per enumerazione filtrata -> usa list_learnings.
        RESTITUISCE: {id, title, category, description, prevention, severity, tags, module, project, session, created_at, updated_at, frequency, last_occurrence} + kg_context se deep=true."""
        try:
            async with acquire_db() as db:
                # DECISION 2: deep KG enrichment is an adapter concern; core fetch
                # here, deep-attach lands in a later F3 increment.
                result = await learnings_uc.get_learning(
                    LOCAL_CTX, db, learning_id=learning_id
                )
                return dump(result)
        except ServiceError as e:
            raise_mcp_error(e)

    @mcp.tool()
    async def delete_learning(
        learning_id: Annotated[str, Field(min_length=1)],
    ) -> dict[str, Any]:
        """Permanently remove a learning from the Marvis DB (non-recoverable) + prune the search index.

        QUANDO USARLO: solo per learning duplicati, spam, o entry chiaramente invalide. Hard delete: rimuove anche il mirror nell'indice di ricerca (documents + vec_documents + FTS) cosi' il learning smette di comparire in search/check_learnings.
        QUANDO NON USARLO: NOT per archiviare un learning ancora valido — la cancellazione e' permanente e perde la history. Aggiornalo con update_learning se il contenuto e' obsoleto.
        RESTITUISCE: {deleted: true} o 404 se non esiste."""
        try:
            async with acquire_write_db(label="mcp.delete_learning") as db:
                await learnings_uc.delete_learning(
                    LOCAL_CTX, db, learning_id=learning_id
                )
                return {"deleted": True}
        except ServiceError as e:
            raise_mcp_error(e)
