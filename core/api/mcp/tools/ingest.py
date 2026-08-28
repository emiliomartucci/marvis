# v1.0.0 - 2026-05-27 - S1 F3.1d: ingest-triage MCP tool group (use_cases-direct, no HTTP)
"""Ingest-triage MCP tools — port of the Node ``*_ingest*`` group, use_cases-direct.

Same TEMPLATE as ``tasks.py`` / ``graph.py``: the Node HTTP proxy
(``get``/``post``/``patch`` -> ``:8100``) is replaced by an in-process
``await ingest_uc.<fn>(current_mcp_context(), db, ...)`` against the read/write pool the
tool acquires via ``acquire_db()`` / ``acquire_write_db()``. Docstrings are copied
VERBATIM from ``core/mcp-pir/index.mjs`` (curated QUANDO USARLO / NON USARLO /
RESTITUISCE blocks).

Schema port (Zod -> Pydantic), per S1 F3:
  * ``z.enum([...])``            -> ``Literal[...]``
  * ``z.string().uuid()``        -> ``Annotated[str, Field(pattern=_UUID_PATTERN)]``
  * ``z.string().regex(slug)``   -> ``Annotated[str, Field(pattern=_PROJECT_SLUG_PATTERN)]``
  * ``z.number().int().min().max()`` -> ``Annotated[int, Field(ge=, le=)]``
  * ``z.record(z.string(), z.string())`` -> ``dict[str, str] | None``
  * optional                     -> ``X | None = None`` (or ``= <default>``)

Visibility: trusted local stdio resolves the explicit ``ws_default`` local context;
remote MCP resolves the authenticated workspace and its current visible projects.
No remote call falls back to the local identity.

POST-TRANSITION SCHEDULING mirrors the HTTP adapter. After the durable use-case
commit, approve schedules ``execute_saga`` and classify/reparse schedule
``parse_pending`` with the authenticated workspace. Imports stay function-local
so importing the MCP registry itself remains lightweight. Recovery remains the
durable fallback if the process dies after commit and before the task completes.

PATCH transport-boundary replication. ``patch_pending`` (the use_case) owns the
sha-collision check, containment, atomic move, and the row + change-history writes;
the HTTP adapter resolves the row + the destination input-root + the slug/path
guards BEFORE calling it. The MCP ``patch_ingest_pending`` surface takes only
``id`` + optional ``project_slug`` (Node parity), so this module replicates the
ADAPTER pre-work locally, fastapi-free: load the row, no-op short-circuit on an
unchanged/empty slug, slug-regex validate, and resolve the destination
``input_root`` against ``PROJECTS_ROOT`` (the same constant the router uses;
test-overridable via ``monkeypatch.setattr(ingest_tools, "PROJECTS_ROOT", ...)``).
The bespoke ``HTTPException`` dict bodies the router raises are a transport contract
the MCP surface does not carry — slug/path failures raise ``ValidationError`` /
``NotFoundError`` (mapped via ``raise_mcp_error``).

SKIPPED (no clean fastapi-free use_case):
  * ``upload_ingest`` — there is NO ``upload`` function in
    ``use_cases.ingest_triage``. The upload pipeline (MIME validation +
    ``_save_upload_file`` + ``dispatch_files_batched``) lives ENTIRELY in
    ``routers/ingest_triage.py`` + the ingest services, and ``dispatch_files_batched``
    transitively imports fastapi. Porting would mean re-implementing the
    router/service multipart+dispatch path, not calling a use_case — out of scope
    for the use_cases-direct port (S1 F3 SKIP rule), so it is skipped, not faked.
  * ``write_haiku_frontmatter`` — the HTTP endpoint is a router-only 501 placeholder
    (``write_frontmatter_placeholder``); there is no ``use_cases.ingest_triage``
    function and nothing to call. The Node tool documents "Pre-E5: 501". Skipped
    until E5 extracts a use_case.
"""
from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import Field

from core.api.mcp._adapter import (
    acquire_db,
    acquire_write_db,
    current_mcp_context,
    current_visible_projects,
    dump,
    raise_mcp_error,
)
from core.api.use_cases import ingest_triage as ingest_uc
from core.api.use_cases._errors import NotFoundError, ServiceError, ValidationError

# Zod enums -> Literals (mirror the Node tool signatures).
IngestStatus = Literal[
    "queued", "parsing", "classified", "awaiting_triage", "approved",
    "inserted", "done", "parse_error", "rejected", "all",
]

# Node Zod patterns mirrored as Pydantic Field patterns.
_UUID_PATTERN = (
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
# index.mjs _PROJECT_SLUG_RE = /^[a-z0-9][a-z0-9_&-]+$/
_PROJECT_SLUG_PATTERN = r"^[a-z0-9][a-z0-9_&-]+$"
_PROJECT_SLUG_RE = re.compile(_PROJECT_SLUG_PATTERN)

# The projects metadata root the patch file-move resolves against. Same constant
# the router uses; module-level so a test can override it with
# ``monkeypatch.setattr(ingest_tools, "PROJECTS_ROOT", tmp_path)``.
PROJECTS_ROOT = Path("/data/projects").resolve()
_EXTRACTED_TEXT_PREVIEW_CHARS = 240
_STRUCTURE_KEY_LIMIT = 20
_CLASSIFICATION_TAG_LIMIT = 8


def _schedule_parse(ingest_id: str, workspace_id: str) -> asyncio.Task[None]:
    from core.api.services.ingest.parser_router import parse_pending

    return asyncio.create_task(parse_pending(ingest_id, workspace_id))


def _schedule_saga(ingest_id: str, workspace_id: str) -> asyncio.Task[None]:
    from core.api.services.ingest.insert_saga import execute_saga

    return asyncio.create_task(execute_saga(ingest_id, workspace_id))


def _project_input_root(project_slug: str) -> Path:
    """Resolve ``PROJECTS_ROOT/<slug>/input`` with the router's containment guards.

    fastapi-free counterpart of the router's ``_project_input_root``: the bespoke
    ``HTTPException`` dict bodies (``INVALID_SLUG_FORMAT`` / ``PROJECT_NOT_FOUND`` /
    ``SLUG_PATH_TRAVERSAL``) are a transport contract the MCP surface does not carry,
    so the same conditions raise ``ValidationError`` / ``NotFoundError`` (mapped via
    ``raise_mcp_error``).
    """
    if not _PROJECT_SLUG_RE.fullmatch(project_slug):
        raise ValidationError(
            code="invalid_slug_format",
            message=f"Project slug '{project_slug}' must match [a-z0-9][a-z0-9_&-]+",
        )
    project_root = (PROJECTS_ROOT / project_slug).resolve()
    if not project_root.is_relative_to(PROJECTS_ROOT):
        raise ValidationError(
            code="slug_path_traversal",
            message=f"Resolved path for '{project_slug}' escapes projects root",
        )
    if not project_root.is_dir():
        raise NotFoundError(
            code="project_not_found",
            message=f"Project '{project_slug}' not found",
        )
    input_root = (project_root / "input").resolve()
    if not input_root.is_relative_to(project_root):
        raise ValidationError(
            code="slug_path_traversal",
            message="Input directory escapes project root",
        )
    return input_root


def _minimize_ingest_item(item: dict[str, Any]) -> dict[str, Any]:
    """Drop heavy parsed content from MCP list responses by default."""
    slim = dict(item)
    extracted_text = slim.pop("extracted_text", None)
    structure = slim.pop("structure", None)
    classification = slim.pop("classification", None)
    if isinstance(extracted_text, str) and extracted_text:
        slim["extracted_text_preview"] = extracted_text[
            :_EXTRACTED_TEXT_PREVIEW_CHARS
        ]
        slim["extracted_text_chars"] = len(extracted_text)
    if isinstance(structure, dict) and structure:
        slim["structure_keys"] = sorted(str(k) for k in structure.keys())[
            :_STRUCTURE_KEY_LIMIT
        ]
    if isinstance(classification, dict) and classification:
        slim["classification_summary"] = _summarize_classification(classification)
    return slim


def _summarize_classification(classification: dict[str, Any]) -> dict[str, Any]:
    """Keep routing-critical classification fields without verbose LLM metadata."""
    keys = (
        "type",
        "document_type",
        "title",
        "target_folder",
        "target_filename",
        "confidence",
        "reason",
        "auto_approve",
        "suggested_project_slug",
    )
    summary = {
        key: classification[key]
        for key in keys
        if key in classification and classification[key] is not None
    }
    tags = classification.get("tags")
    if isinstance(tags, list) and tags:
        summary["tags"] = [str(tag) for tag in tags[:_CLASSIFICATION_TAG_LIMIT]]
        if len(tags) > _CLASSIFICATION_TAG_LIMIT:
            summary["tags_overflow_count"] = len(tags) - _CLASSIFICATION_TAG_LIMIT
    return summary


def register(mcp) -> None:
    """Register the ingest-triage tool group on the shared FastMCP instance."""

    @mcp.tool()
    async def list_ingest_pending(
        status: IngestStatus | None = None,
        project: Annotated[str, Field(pattern=_PROJECT_SLUG_PATTERN)] | None = None,
        limit: Annotated[int, Field(ge=1, le=250)] = 50,
        include_content: bool = False,
    ) -> list[dict[str, Any]]:
        """List ingest_pending rows con filtri status/project/limit. Phase 1.5 capability via MCP per agent.

        QUANDO USARLO: serve la coda triage (queued/parsing/awaiting_triage/parse_error/...) per un progetto o globale (visibility-filtered).
        QUANDO NON USARLO: NOT per leggere testo estratto completo salvo debug esplicito con include_content=true. NOT per cambiare stato -> approve/reject/patch.
        RESTITUISCE: list slim by default (id, file_path, project_slug, status, classification, target_folder, target_filename, previews/metadata). include_content=true include extracted_text/structure."""
        # Node maps status="all" -> no status filter (drops the param). Local sees
        # all projects (visible_projects=None), so project_filter_visible stays True.
        try:
            async with acquire_db() as db:
                ctx = current_mcp_context()
                visible_projects = await current_visible_projects(db, ctx)
                result = await ingest_uc.list_pending(
                    ctx,
                    db,
                    status=None if status == "all" else status,
                    project_slug=project,
                    limit=limit,
                    visible_projects=visible_projects,
                    project_filter_visible=(
                        project is None
                        or visible_projects is None
                        or project in visible_projects
                    ),
                )
                payload = dump(result)
                if include_content:
                    return payload
                return [_minimize_ingest_item(item) for item in payload]
        except ServiceError as e:
            raise_mcp_error(e)

    @mcp.tool()
    async def approve_ingest_pending(
        id: Annotated[str, Field(pattern=_UUID_PATTERN)],
        classification_override: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Approve ingest_pending row → committa la row a status=approved. Il move del file da input/ a target_folder + l'insert nel KG avvengono in modo ASINCRONO dal worker ingest (server-lifecycle), NON in questa chiamata.

        QUANDO USARLO: row in awaiting_triage con target_folder/target_filename validi → approval umano via agent.
        QUANDO NON USARLO: NOT su row in stato diverso da awaiting_triage (viene rifiutata). NOT per cambiare project_slug -> usa patch_ingest_pending.
        NOTA: `classification_override` e' accettato per compatibilita' ma NON inoltrato / no-op su questa superficie (backend support post-E5).
        RESTITUISCE: {id, status:approved}."""
        # The use_case returns (response, project_slug). classification_override is
        # accepted for Node parity but not forwarded — the use_case signature does
        # not carry it (backend support is post-E5). The durable status='approved'
        # transition commits before the same-workspace saga is scheduled.
        try:
            async with acquire_write_db() as db:
                ctx = current_mcp_context()
                visible_projects = await current_visible_projects(db, ctx)
                response, _project_slug = await ingest_uc.approve(
                    ctx, db, ingest_id=id, visible_projects=visible_projects,
                )
                _schedule_saga(id, ctx.workspace_id)
                return dump(response)
        except ServiceError as e:
            raise_mcp_error(e)

    @mcp.tool()
    async def reject_ingest_pending(
        id: Annotated[str, Field(pattern=_UUID_PATTERN)],
        reason: Annotated[str, Field(max_length=500)] | None = None,
    ) -> dict[str, Any]:
        """Reject ingest_pending row → status=rejected, file resta orphan in input/.

        QUANDO USARLO: row non vuole essere ingestita (low signal, duplicato, off-topic) → libera la coda triage.
        QUANDO NON USARLO: NOT per cancellare il file fisico (resta orphan in input/). NOT per cambiare project_slug post-rejection -> patch_ingest_pending supporta lo stato rejected.
        NOTA: `reason` e' opzionale e viene accettato ma NON persistito su questa superficie (la transizione registra solo triage_decision_id).
        RESTITUISCE: {id, status:rejected}."""
        # The use_case `reject` is a soft state transition and does not carry a
        # `reason` arg (the Node `reason` is the HTTP body the legacy router
        # accepted; the durable transition only records `triage_decision_id`).
        # Accepted on the surface for Node parity. Returns (response, project_slug).
        try:
            async with acquire_write_db() as db:
                ctx = current_mcp_context()
                visible_projects = await current_visible_projects(db, ctx)
                response, _project_slug = await ingest_uc.reject(
                    ctx, db, ingest_id=id, visible_projects=visible_projects,
                )
                return dump(response)
        except ServiceError as e:
            raise_mcp_error(e)

    @mcp.tool()
    async def patch_ingest_pending(
        id: Annotated[str, Field(pattern=_UUID_PATTERN)],
        project_slug: Annotated[str, Field(pattern=_PROJECT_SLUG_PATTERN)] | None = None,
    ) -> dict[str, Any]:
        """Change project_slug post-upload (E4). State machine: SOLO awaiting_triage/parse_error/rejected. Atomic copy-then-rename con path containment.

        QUANDO USARLO: file finito nel progetto sbagliato in upload → reroute prima dell'approve.
        QUANDO NON USARLO: NOT su row in flight (queued/parsing/approved/inserted/done) -> 422. NOT per cambiare classification (target_folder/filename) -> richiede E4 follow-up non ancora live.
        RESTITUISCE: IngestPendingItem aggiornato."""
        # Replicate the router's transport-boundary pre-work fastapi-free (load row,
        # state guard, no-op short-circuit, slug validate, input_root resolve), then
        # hand the resolved Path objects to the use_case which owns the domain truth
        # (sha-collision, containment, atomic move, row + change-history writes).
        try:
            async with acquire_write_db() as db:
                ctx = current_mcp_context()
                visible_projects = await current_visible_projects(db, ctx)
                row = await ingest_uc._visible_row(ctx, db, id, visible_projects)

                allowed_states = ("awaiting_triage", "parse_error", "rejected")
                if row["status"] not in allowed_states:
                    raise ValidationError(
                        code="invalid_state_for_change",
                        message=(
                            f"Item state '{row['status']}' is not patchable "
                            f"(allowed: {', '.join(allowed_states)})"
                        ),
                    )

                new_slug = project_slug
                if not new_slug or new_slug == row["project_slug"]:
                    # No-op: return the current row so the caller can refresh.
                    return dump(ingest_uc._row_to_item(row))

                ingest_uc._enforce_visibility(new_slug, visible_projects)
                new_input_root = _project_input_root(new_slug)
                result = await ingest_uc.patch_pending(
                    ctx,
                    db,
                    row=row,
                    new_slug=new_slug,
                    new_input_root=new_input_root,
                    projects_root=PROJECTS_ROOT,
                    changed_by=ctx.username,
                    source_ip=None,
                    user_agent="mcp-local",
                )
                return dump(result)
        except ServiceError as e:
            raise_mcp_error(e)

    @mcp.tool()
    async def classify_ingest(
        id: Annotated[str, Field(pattern=_UUID_PATTERN)],
        force: bool = False,
    ) -> dict[str, Any]:
        """Force re-classify ingest_pending row. Phase 1 = deterministic re-parse (parser_router); post P1.5.E5 = trigger Haiku #1 manuale senza cambi MCP/UI.

        QUANDO USARLO: classification originale era sbagliata e vuoi rilanciare il classifier (es. dopo training data update).
        QUANDO NON USARLO: NOT per parse_error (usa reparse_ingest che e' lo stesso path semanticamente). NOT su done/inserted (stato non valido, rifiutato).
        NOTA: `force` e' accettato ma no-op (il re-classify e' sempre forzato; il flag non cambia il comportamento). Il re-parse effettivo lo esegue il worker ingest async; qui la row viene resettata a queued.
        RESTITUISCE: {id, status:parser_waiting}."""
        # `force` is accepted for Node parity; the Node handler ignores it too (it
        # always POSTs classify-force). The durable status='queued' reset commits
        # before the deterministic same-workspace re-parse is scheduled.
        try:
            async with acquire_write_db() as db:
                ctx = current_mcp_context()
                visible_projects = await current_visible_projects(db, ctx)
                result = await ingest_uc.classify_force(
                    ctx, db, ingest_id=id, visible_projects=visible_projects,
                )
                _schedule_parse(id, ctx.workspace_id)
                return dump(result)
        except ServiceError as e:
            raise_mcp_error(e)

    @mcp.tool()
    async def reparse_ingest(
        id: Annotated[str, Field(pattern=_UUID_PATTERN)] | None = None,
        status: Literal["parse_error"] | None = None,
    ) -> dict[str, Any]:
        """Re-parse ingest_pending row (single id) o batch (status=parse_error, admin only). Riusa parser_router via API invece di scripts/reparse_failed.py.

        QUANDO USARLO: row in parse_error → ritenta dopo fix parser/dipendenze. Batch utile per recovery post-deploy.
        QUANDO NON USARLO: NOT con id E status insieme (usa uno o l'altro). NOT per cambiare project_slug -> patch_ingest_pending.
        RESTITUISCE: single → {id, status:parser_waiting}; batch → {queued_count, status} (status = target_status, es. parse_error)."""
        if id and status:
            raise_mcp_error(
                ValidationError(
                    code="reparse_args_conflict",
                    message="Provide id OR status, not both",
                )
            )
        try:
            if id:
                async with acquire_write_db() as db:
                    ctx = current_mcp_context()
                    visible_projects = await current_visible_projects(db, ctx)
                    result = await ingest_uc.reparse_single(
                        ctx, db, ingest_id=id, visible_projects=visible_projects,
                    )
                    _schedule_parse(id, ctx.workspace_id)
                    return dump(result)
            if status:
                # The batch read selects matching ids and commits their transitions;
                # schedule one same-workspace parser task for every returned id.
                async with acquire_db() as db:
                    ctx = current_mcp_context()
                    visible_projects = await current_visible_projects(db, ctx)
                    response, ids = await ingest_uc.reparse_batch(
                        ctx,
                        db,
                        target_status=status,
                        visible_projects=visible_projects,
                    )
                    for ingest_id in ids:
                        _schedule_parse(ingest_id, ctx.workspace_id)
                    return dump(response)
            raise_mcp_error(
                ValidationError(
                    code="reparse_args_missing",
                    message="Either id or status required",
                )
            )
        except ServiceError as e:
            raise_mcp_error(e)
