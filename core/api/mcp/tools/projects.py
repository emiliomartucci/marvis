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

import asyncio
from datetime import datetime
import logging
from typing import Annotated, Any

import aiosqlite
from pydantic import Field

from core.api.mcp._adapter import (
    acquire_db,
    acquire_write_db,
    attach_notices,
    current_mcp_context,
    current_visible_projects,
    dump,
    raise_mcp_error,
    require_any_grant,
    require_unambiguous_visible_project,
)
from core.api.services import project_lifecycle as lifecycle_service
from core.api.services import governed_decisions as decision_service
from core.api.use_cases import projects as projects_uc
from core.api.use_cases._context import CallerContext, require_role_ctx
from core.api.use_cases._errors import ServiceError

logger = logging.getLogger(__name__)

# Background task set for the fire-and-forget project embeds (same GC-prevention
# pattern as _adapter._bg_embed_tasks: asyncio holds only a weak reference to a
# bare create_task() result).
_bg_embed_projects: set[asyncio.Task] = set()


def _schedule_embed_project_mcp(
    *,
    slug: str,
    description: str | None,
    workspace_id: str,
) -> None:
    """MCP-local twin of ``routers.projects._schedule_embed_project`` (fastapi-free).

    Embed-on-write so a just-created project is immediately searchable by meaning.
    Fire-and-forget on the running tool loop — the embed body re-acquires the
    single-writer lock itself, so it must NEVER be awaited while the create still
    holds the writer (learning f83f5209). Keying matches the router seam /
    ``use_cases.search._reindex_projects``: ``description or ""`` is the embedded
    description and ``desc or slug`` the doc title. No-ops when the embedder is
    unavailable.
    """
    from core.api.services import embedding_service

    if not embedding_service.is_available():
        return

    desc = description or ""
    name = desc or slug

    async def _embed() -> None:
        try:
            await embedding_service.embed_project_document(
                slug=slug,
                name=name,
                description=desc,
                workspace_id=workspace_id,
            )
        except Exception:
            logger.debug(
                "MCP auto-embed project %s failed (non-critical)", slug, exc_info=True
            )

    try:
        t = asyncio.create_task(_embed())
    except RuntimeError:
        logger.debug("MCP auto-embed project skipped: no running event loop")
        return
    _bg_embed_projects.add(t)
    t.add_done_callback(_bg_embed_projects.discard)


async def create_project_impl(
    db: aiosqlite.Connection,
    ctx: CallerContext,
    *,
    slug: str,
    name: str | None,
    description: str | None,
    owner: str | None,
) -> dict[str, Any]:
    """Body of the ``create_project`` tool (db + ctx explicit for unit tests).

    Same injection the HTTP router does at ``routers/projects.py`` POST: the
    use_case owns RBAC (operator+), slug conflicts, the disk write (router
    helper, reached function-locally) and the creator=project-admin grant
    (RBAC F2.6 — persons automatically, service callers via ``owner``).
    """
    result = await projects_uc.create_project(
        ctx,
        db,
        slug=slug,
        name=name,
        program=None,
        scope=None,
        description=description,
        lifecycle="idea",
        language=None,
        type="work",
        owner=owner,
    )
    return dump(result)


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
                ctx = current_mcp_context()
                visible_projects = await current_visible_projects(db, ctx)
                require_any_grant(visible_projects)
                # Node `list_projects` returns the program-grouped project list =
                # `list_programs` here. visible_projects=None -> local sees all.
                result = await projects_uc.list_programs(
                    ctx,
                    db,
                    visible_projects=visible_projects,
                    include_archived=lifecycle == "archived",
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
        NOTA: il parametro deep=true e' accettato ma oggi IGNORATO (nessun kg_context; enrichment differito).
        RESTITUISCE: {slug, metadata, context_md, handoffs[], docs[], deploy_info}."""
        try:
            async with acquire_db() as db:
                ctx = current_mcp_context()
                visible_projects = await current_visible_projects(db, ctx)
                # DECISION 2: `deep` KG enrichment is an adapter concern; the
                # use_case returns kg_context=None (core fetch). The deep-attach
                # is a later F3 increment, same as get_task/get_learning in F3.0.
                result = await projects_uc.get_project(
                    ctx, db, slug=slug, visible_projects=visible_projects
                )
                return await attach_notices(
                    db, dump(result), ctx, visible_projects=visible_projects, project=slug
                )
        except ServiceError as e:
            raise_mcp_error(e)

    @mcp.tool()
    async def session_brief(
        slug: Annotated[str, Field(min_length=1, max_length=50)],
    ) -> dict[str, Any]:
        """Cold-start bundle for one project.

        QUANDO USARLO: prima call su uno slug; sostituisce get_project + list_tasks + handoff/learnings. CANONICALITY: metadata_path e' la fonte hosted per lo STATO; repo_path e' un graph-only mirror (vedi repo_path_nature nel payload) — NON usarlo come base di lavoro git: lavora da clone locale, branch feat/task-<id>, push GitHub.
        QUANDO NON USARLO: body context.md/docs completi -> get_project; filtri puntuali -> list_tasks/list_handoffs.
        PROVA: stato progetto, task aperti, latest_handoff, learnings e repo_path hosted (graph-only).
        NEXT: check_learnings prima di codice/deploy/reindex.
        RESTITUISCE: {project (include repo_path, repo_path_nature, metadata_path), open_tasks[], latest_handoff, recent_learnings[], top_salience_docs[]}; il payload include anche notices quando il caller e' una persona con notifiche non lette."""
        try:
            async with acquire_db() as db:
                ctx = current_mcp_context()
                visible_projects = await current_visible_projects(db, ctx)
                require_any_grant(visible_projects)
                # The bundle's kg_context is part of the contract (assembled inside
                # the use_case, not deferred). Node cold-starts deep by default
                # (`effectiveDeep` -> true), so widen the per-bucket KG limits.
                result = await projects_uc.get_session_brief(
                    ctx, db, slug=slug, deep=True, visible_projects=visible_projects
                )
                return await attach_notices(
                    db, dump(result), ctx, visible_projects=visible_projects, project=slug
                )
        except ServiceError as e:
            raise_mcp_error(e)

    @mcp.tool()
    async def create_project(
        slug: Annotated[
            str,
            Field(min_length=1, max_length=63, pattern=r"^[a-z0-9][a-z0-9&+_.\-]{0,62}$"),
        ],
        name: Annotated[str, Field(max_length=100)] | None = None,
        description: Annotated[str, Field(max_length=500)] | None = None,
        owner: Annotated[str, Field(max_length=200)] | None = None,
    ) -> dict[str, Any]:
        """Crea un nuovo progetto work sul tenant (operator+).

        QUANDO USARLO: serve un progetto nuovo (directory + project.yaml + context.md + .task). Se sei una persona diventi project-admin del progetto (creator grant F2.6); un caller bearer/agent puo' nominare owner (user_id/slug/email) per assegnare quel grant.
        QUANDO NON USARLO: NOT per progetti esistenti -> session_brief/get_project. NOT per dare accessi dopo la creazione -> grant_access o assign_team_project.
        RESTITUISCE: {slug, name, program, lifecycle, type, metadata_path}; errori project_exists / project_dir_exists su slug gia' usato.
        NEXT: assign_team_project o grant_access per aprire l'accesso al team."""
        try:
            async with acquire_write_db(label="mcp.create_project") as db:
                ctx = current_mcp_context()
                result = await create_project_impl(
                    db, ctx, slug=slug, name=name, description=description, owner=owner
                )
                # Embed-on-write parity with the HTTP surface: fire-and-forget,
                # scheduled AFTER the use_case returns (never awaited under the
                # writer — the embed body re-acquires the write lock itself).
                _schedule_embed_project_mcp(
                    slug=result["slug"],
                    description=result.get("description"),
                    workspace_id=ctx.workspace_id,
                )
                return result
        except ServiceError as e:
            raise_mcp_error(e)

    @mcp.tool()
    async def register_project_lifecycle(
        slug: Annotated[str, Field(min_length=1, max_length=63)],
    ) -> dict[str, Any]:
        """Register an existing project with an immutable lifecycle identity.

        QUANDO USARLO: preparazione amministrativa prima di approval/archive.
        QUANDO NON USARLO: NOT come archive e NOT per cambiare project.yaml.
        RESTITUISCE: project_id, lifecycle, digest e writer watermark."""
        try:
            async with acquire_write_db(label="mcp.register_project_lifecycle") as db:
                ctx = current_mcp_context()
                require_role_ctx(ctx, "operator", "admin", "super_admin")
                visible_projects = await current_visible_projects(db, ctx)
                await require_unambiguous_visible_project(
                    db, ctx, slug, visible_projects
                )
                async with lifecycle_service.async_project_mutation_guard():
                    snapshot = await lifecycle_service.ensure_project_lifecycle(
                        db,
                        workspace_id=ctx.workspace_id,
                        project_slug=slug,
                    )
                    await db.commit()
                return lifecycle_service.snapshot_dict(snapshot)
        except ServiceError as e:
            raise_mcp_error(e)

    @mcp.tool()
    async def get_project_lifecycle(
        slug: Annotated[str, Field(min_length=1, max_length=63)],
    ) -> dict[str, Any]:
        """Read project lifecycle by known slug, including archived history.

        QUANDO USARLO: readback before retry or after archive.
        QUANDO NON USARLO: NOT to discover selectable projects -> list_projects.
        RESTITUISCE: immutable ID, lifecycle, digests and transition state."""
        try:
            async with acquire_db() as db:
                ctx = current_mcp_context()
                visible_projects = await current_visible_projects(db, ctx)
                await require_unambiguous_visible_project(
                    db, ctx, slug, visible_projects
                )
                snapshot = await lifecycle_service.read_project_lifecycle(
                    db,
                    workspace_id=ctx.workspace_id,
                    project_slug=slug,
                )
                return lifecycle_service.snapshot_dict(snapshot)
        except ServiceError as e:
            raise_mcp_error(e)

    @mcp.tool()
    async def get_cloud_f_control() -> dict[str, Any]:
        """Read the common Cloud/F epoch, lease and active-operation set.

        QUANDO USARLO: bind a protected change or archive approval to live state.
        QUANDO NON USARLO: NOT as proof that U11 entry points are deployed.
        RESTITUISCE: readiness, epoch, lease and active-operations digest."""
        try:
            async with acquire_db() as db:
                ctx = current_mcp_context()
                snapshot = await lifecycle_service.read_cloud_f_control(
                    db,
                    workspace_id=ctx.workspace_id,
                )
                return lifecycle_service.snapshot_dict(snapshot)
        except ServiceError as e:
            raise_mcp_error(e)

    @mcp.tool()
    async def activate_cloud_f_control(
        subtype: Annotated[
            str,
            Field(pattern=r"^(bootstrap_activation|existing_live_adoption)$"),
        ],
        expected_epoch: Annotated[int, Field(ge=0)],
    ) -> dict[str, Any]:
        """Activate/adopt the common lease exactly once (U11 readiness only).

        QUANDO USARLO: only through the master cloud-bootstrap operation.
        QUANDO NON USARLO: NOT during source preparation or Plan F mutation.
        RESTITUISCE: activated control readback; approval authority required."""
        try:
            async with acquire_write_db(label="mcp.activate_cloud_f_control") as db:
                snapshot = await lifecycle_service.activate_cloud_f_control(
                    current_mcp_context(),
                    db,
                    subtype=subtype,
                    expected_epoch=expected_epoch,
                )
                return lifecycle_service.snapshot_dict(snapshot)
        except ServiceError as e:
            raise_mcp_error(e)

    @mcp.tool()
    async def acquire_cloud_f_change(
        operation_id: Annotated[str, Field(min_length=1, max_length=128)],
        operation_kind: Annotated[str, Field(min_length=1, max_length=100)],
        expected_epoch: Annotated[int, Field(ge=0)],
        lease_expires_at: datetime,
    ) -> dict[str, Any]:
        """Acquire the exclusive common lease for one protected U11 operation."""
        try:
            async with acquire_write_db(label="mcp.acquire_cloud_f_change") as db:
                snapshot = await lifecycle_service.acquire_cloud_f_change(
                    current_mcp_context(),
                    db,
                    operation_id=operation_id,
                    operation_kind=operation_kind,
                    expected_epoch=expected_epoch,
                    lease_expires_at=lease_expires_at,
                )
                return lifecycle_service.snapshot_dict(snapshot)
        except ServiceError as e:
            raise_mcp_error(e)

    @mcp.tool()
    async def complete_cloud_f_change(
        operation_id: Annotated[str, Field(min_length=1, max_length=128)],
        expected_epoch: Annotated[int, Field(ge=0)],
        advance_epoch: bool = True,
    ) -> dict[str, Any]:
        """Release the exact Cloud/F lease and normally advance its epoch."""
        try:
            async with acquire_write_db(label="mcp.complete_cloud_f_change") as db:
                snapshot = await lifecycle_service.complete_cloud_f_change(
                    current_mcp_context(),
                    db,
                    operation_id=operation_id,
                    expected_epoch=expected_epoch,
                    advance_epoch=advance_epoch,
                )
                return lifecycle_service.snapshot_dict(snapshot)
        except ServiceError as e:
            raise_mcp_error(e)

    @mcp.tool()
    async def get_cloud_f_change_operation(
        operation_id: Annotated[str, Field(min_length=1, max_length=128)],
    ) -> dict[str, Any]:
        """Read a durable protected-change operation before retrying it."""
        try:
            async with acquire_db() as db:
                ctx = current_mcp_context()
                return await lifecycle_service.read_cloud_f_change_operation(
                    db,
                    workspace_id=ctx.workspace_id,
                    operation_id=operation_id,
                )
        except ServiceError as e:
            raise_mcp_error(e)

    @mcp.tool()
    async def update_project_selector_watermark(
        project_slug: Annotated[str, Field(min_length=1, max_length=63)],
        operation_id: Annotated[str, Field(min_length=1, max_length=128)],
        expected_epoch: Annotated[int, Field(ge=0)],
        expected_selector_watermark: Annotated[str, Field(max_length=256)],
        selector_watermark: Annotated[
            str, Field(pattern=r"^[0-9a-f]{64}$")
        ],
    ) -> dict[str, Any]:
        """Bind a selector snapshot while the caller owns the common lease."""
        try:
            async with acquire_write_db(
                label="mcp.update_project_selector_watermark"
            ) as db:
                ctx = current_mcp_context()
                visible_projects = await current_visible_projects(db, ctx)
                await require_unambiguous_visible_project(
                    db, ctx, project_slug, visible_projects
                )
                snapshot = (
                    await lifecycle_service.update_project_selector_watermark(
                        ctx,
                        db,
                        project_slug=project_slug,
                        operation_id=operation_id,
                        expected_epoch=expected_epoch,
                        expected_selector_watermark=expected_selector_watermark,
                        selector_watermark=selector_watermark,
                    )
                )
                return lifecycle_service.snapshot_dict(snapshot)
        except ServiceError as e:
            raise_mcp_error(e)

    @mcp.tool()
    async def create_project_archive_approval(
        project_slug: Annotated[str, Field(min_length=1, max_length=63)],
        expected_project_id: Annotated[str, Field(min_length=5, max_length=80)],
        expected_project_digest: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")],
        plan_f_digest: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")],
        master_digest: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")],
        evidence_digest: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")],
        expected_writer_watermark: Annotated[int, Field(ge=0)],
        expected_selector_watermark: Annotated[str, Field(max_length=256)],
        expected_cloud_f_epoch: Annotated[int, Field(ge=0)],
        expected_active_operations_digest: Annotated[
            str, Field(pattern=r"^[0-9a-f]{64}$")
        ],
        expires_at: datetime,
        approval_id: Annotated[str | None, Field(max_length=128)] = None,
    ) -> dict[str, Any]:
        """Record explicit approval bound to every archive compare-and-set value.

        Agent MCP calls require a live persisted delegation; a caller-provided
        approval string never creates authority."""
        try:
            async with acquire_write_db(label="mcp.create_archive_approval") as db:
                ctx = current_mcp_context()
                visible_projects = await current_visible_projects(db, ctx)
                await require_unambiguous_visible_project(
                    db, ctx, project_slug, visible_projects
                )
                return await lifecycle_service.create_archive_approval(
                    ctx,
                    db,
                    project_slug=project_slug,
                    expected_project_id=expected_project_id,
                    expected_project_digest=expected_project_digest,
                    plan_f_digest=plan_f_digest,
                    master_digest=master_digest,
                    evidence_digest=evidence_digest,
                    expected_writer_watermark=expected_writer_watermark,
                    expected_selector_watermark=expected_selector_watermark,
                    expected_cloud_f_epoch=expected_cloud_f_epoch,
                    expected_active_operations_digest=(
                        expected_active_operations_digest
                    ),
                    expires_at=expires_at,
                    approval_id=approval_id,
                )
        except ServiceError as e:
            raise_mcp_error(e)

    @mcp.tool()
    async def archive_project(
        project_slug: Annotated[str, Field(min_length=1, max_length=63)],
        project_id: Annotated[str, Field(min_length=5, max_length=80)],
        approval_id: Annotated[str, Field(min_length=1, max_length=128)],
        expected_project_digest: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")],
        plan_f_digest: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")],
        master_digest: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")],
        evidence_digest: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")],
        expected_writer_watermark: Annotated[int, Field(ge=0)],
        expected_selector_watermark: Annotated[str, Field(max_length=256)],
        expected_cloud_f_epoch: Annotated[int, Field(ge=0)],
        expected_active_operations_digest: Annotated[
            str, Field(pattern=r"^[0-9a-f]{64}$")
        ],
        operation_id: Annotated[str, Field(min_length=1, max_length=128)],
        idempotency_key: Annotated[str, Field(min_length=1, max_length=200)],
        lease_expires_at: datetime,
    ) -> dict[str, Any]:
        """Archive one logical project through the approval-bound saga.

        QUANDO USARLO: only after U11 readiness and explicit bound approval.
        QUANDO NON USARLO: NOT to delete/move tenant data or runtime assets.
        RESTITUISCE: immutable project/readback coordinates; safe to replay."""
        try:
            async with acquire_write_db(label="mcp.archive_project") as db:
                ctx = current_mcp_context()
                visible_projects = await current_visible_projects(db, ctx)
                await require_unambiguous_visible_project(
                    db, ctx, project_slug, visible_projects
                )
                return await lifecycle_service.archive_project(
                    ctx,
                    db,
                    project_slug=project_slug,
                    project_id=project_id,
                    approval_id=approval_id,
                    expected_project_digest=expected_project_digest,
                    plan_f_digest=plan_f_digest,
                    master_digest=master_digest,
                    evidence_digest=evidence_digest,
                    expected_writer_watermark=expected_writer_watermark,
                    expected_selector_watermark=expected_selector_watermark,
                    expected_cloud_f_epoch=expected_cloud_f_epoch,
                    expected_active_operations_digest=(
                        expected_active_operations_digest
                    ),
                    operation_id=operation_id,
                    idempotency_key=idempotency_key,
                    lease_expires_at=lease_expires_at,
                )
        except ServiceError as e:
            raise_mcp_error(e)

    @mcp.tool()
    async def get_project_lifecycle_operation(
        project_slug: Annotated[str, Field(min_length=1, max_length=63)],
        operation_id: Annotated[str, Field(min_length=1, max_length=128)],
    ) -> dict[str, Any]:
        """Read an archive operation before retrying after a timeout."""
        try:
            async with acquire_db() as db:
                ctx = current_mcp_context()
                visible_projects = await current_visible_projects(db, ctx)
                await require_unambiguous_visible_project(
                    db, ctx, project_slug, visible_projects
                )
                result = await lifecycle_service.read_lifecycle_operation(
                    db,
                    workspace_id=ctx.workspace_id,
                    operation_id=operation_id,
                )
                if result["project_slug"] != project_slug:
                    raise ServiceError(
                        code="lifecycle_operation_scope_mismatch",
                        message="Lifecycle operation does not belong to this project",
                    )
                return result
        except ServiceError as e:
            raise_mcp_error(e)

    @mcp.tool()
    async def create_governed_decision(
        project_slug: Annotated[str, Field(min_length=1, max_length=63)],
        relative_path: Annotated[str, Field(min_length=1, max_length=512)],
        title: Annotated[str, Field(min_length=1, max_length=200)],
        body: Annotated[str, Field(max_length=500_000)],
        operation_id: Annotated[str, Field(min_length=1, max_length=128)],
        idempotency_key: Annotated[str, Field(min_length=1, max_length=200)],
        expected_cloud_f_epoch: Annotated[int, Field(ge=0)],
        lease_expires_at: datetime,
        decision_id: Annotated[str | None, Field(max_length=128)] = None,
    ) -> dict[str, Any]:
        """Create a draft decision through the protected, resumable saga.

        Requires a ready Cloud/F authority and advances its epoch exactly once.
        On timeout, read the operation before replaying the same IDs."""
        try:
            async with acquire_write_db(label="mcp.create_governed_decision") as db:
                ctx = current_mcp_context()
                visible_projects = await current_visible_projects(db, ctx)
                await require_unambiguous_visible_project(
                    db, ctx, project_slug, visible_projects
                )
                return await decision_service.create_decision(
                    ctx,
                    db,
                    project_slug=project_slug,
                    relative_path=relative_path,
                    title=title,
                    body=body,
                    operation_id=operation_id,
                    idempotency_key=idempotency_key,
                    expected_cloud_f_epoch=expected_cloud_f_epoch,
                    lease_expires_at=lease_expires_at,
                    decision_id=decision_id,
                )
        except ServiceError as e:
            raise_mcp_error(e)

    @mcp.tool()
    async def get_governed_decision(
        project_slug: Annotated[str, Field(min_length=1, max_length=63)],
        decision_id: Annotated[str, Field(min_length=1, max_length=128)],
    ) -> dict[str, Any]:
        """Read a known decision directly, including superseded archived history."""
        try:
            async with acquire_db() as db:
                ctx = current_mcp_context()
                visible_projects = await current_visible_projects(db, ctx)
                await require_unambiguous_visible_project(
                    db, ctx, project_slug, visible_projects
                )
                return await decision_service.read_decision(
                    db,
                    workspace_id=ctx.workspace_id,
                    project_slug=project_slug,
                    decision_id=decision_id,
                )
        except ServiceError as e:
            raise_mcp_error(e)

    @mcp.tool()
    async def accept_governed_decision(
        project_slug: Annotated[str, Field(min_length=1, max_length=63)],
        decision_id: Annotated[str, Field(min_length=1, max_length=128)],
        expected_content_digest: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")],
        operation_id: Annotated[str, Field(min_length=1, max_length=128)],
        idempotency_key: Annotated[str, Field(min_length=1, max_length=200)],
        expected_cloud_f_epoch: Annotated[int, Field(ge=0)],
        lease_expires_at: datetime,
    ) -> dict[str, Any]:
        """Accept a draft while preserving its substantive body."""
        try:
            async with acquire_write_db(label="mcp.accept_governed_decision") as db:
                ctx = current_mcp_context()
                visible_projects = await current_visible_projects(db, ctx)
                await require_unambiguous_visible_project(
                    db, ctx, project_slug, visible_projects
                )
                return await decision_service.accept_decision(
                    ctx,
                    db,
                    project_slug=project_slug,
                    decision_id=decision_id,
                    expected_content_digest=expected_content_digest,
                    operation_id=operation_id,
                    idempotency_key=idempotency_key,
                    expected_cloud_f_epoch=expected_cloud_f_epoch,
                    lease_expires_at=lease_expires_at,
                )
        except ServiceError as e:
            raise_mcp_error(e)

    @mcp.tool()
    async def supersede_governed_decision(
        source_project_slug: Annotated[str, Field(min_length=1, max_length=63)],
        source_decision_id: Annotated[str, Field(min_length=1, max_length=128)],
        source_relative_path: Annotated[str, Field(min_length=1, max_length=512)],
        expected_source_content_digest: Annotated[
            str, Field(pattern=r"^[0-9a-f]{64}$")
        ],
        expected_source_body_digest: Annotated[
            str, Field(pattern=r"^[0-9a-f]{64}$")
        ],
        target_project_slug: Annotated[str, Field(min_length=1, max_length=63)],
        target_decision_id: Annotated[str, Field(min_length=1, max_length=128)],
        expected_target_content_digest: Annotated[
            str, Field(pattern=r"^[0-9a-f]{64}$")
        ],
        operation_id: Annotated[str, Field(min_length=1, max_length=128)],
        idempotency_key: Annotated[str, Field(min_length=1, max_length=200)],
        expected_cloud_f_epoch: Annotated[int, Field(ge=0)],
        lease_expires_at: datetime,
    ) -> dict[str, Any]:
        """Supersede an accepted/legacy decision with an accepted target."""
        try:
            async with acquire_write_db(label="mcp.supersede_governed_decision") as db:
                ctx = current_mcp_context()
                visible_projects = await current_visible_projects(db, ctx)
                await require_unambiguous_visible_project(
                    db, ctx, source_project_slug, visible_projects
                )
                await require_unambiguous_visible_project(
                    db, ctx, target_project_slug, visible_projects
                )
                return await decision_service.supersede_decision(
                    ctx,
                    db,
                    source_project_slug=source_project_slug,
                    source_decision_id=source_decision_id,
                    source_relative_path=source_relative_path,
                    expected_source_content_digest=expected_source_content_digest,
                    expected_source_body_digest=expected_source_body_digest,
                    target_project_slug=target_project_slug,
                    target_decision_id=target_decision_id,
                    expected_target_content_digest=expected_target_content_digest,
                    operation_id=operation_id,
                    idempotency_key=idempotency_key,
                    expected_cloud_f_epoch=expected_cloud_f_epoch,
                    lease_expires_at=lease_expires_at,
                )
        except ServiceError as e:
            raise_mcp_error(e)

    @mcp.tool()
    async def create_historical_pointer(
        source_project_slug: Annotated[str, Field(min_length=1, max_length=63)],
        source_kind: Annotated[
            str, Field(pattern=r"^(decision|handoff|learning)$")
        ],
        source_ref: Annotated[str, Field(min_length=1, max_length=512)],
        expected_source_body_digest: Annotated[
            str, Field(pattern=r"^[0-9a-f]{64}$")
        ],
        relation: Annotated[str, Field(pattern=r"^(forward|applies_to)$")],
        target_project_slug: Annotated[str, Field(min_length=1, max_length=63)],
        operation_id: Annotated[str, Field(min_length=1, max_length=128)],
        idempotency_key: Annotated[str, Field(min_length=1, max_length=200)],
        expected_cloud_f_epoch: Annotated[int, Field(ge=0)],
        lease_expires_at: datetime,
        target_decision_id: Annotated[str | None, Field(max_length=128)] = None,
        target_relative_path: Annotated[str | None, Field(max_length=512)] = None,
    ) -> dict[str, Any]:
        """Add handoff/learning/decision lineage without rewriting the source."""
        try:
            async with acquire_write_db(label="mcp.create_historical_pointer") as db:
                ctx = current_mcp_context()
                visible_projects = await current_visible_projects(db, ctx)
                await require_unambiguous_visible_project(
                    db, ctx, source_project_slug, visible_projects
                )
                await require_unambiguous_visible_project(
                    db, ctx, target_project_slug, visible_projects
                )
                return await decision_service.create_historical_pointer(
                    ctx,
                    db,
                    source_project_slug=source_project_slug,
                    source_kind=source_kind,
                    source_ref=source_ref,
                    expected_source_body_digest=expected_source_body_digest,
                    relation=relation,
                    target_project_slug=target_project_slug,
                    target_decision_id=target_decision_id,
                    target_relative_path=target_relative_path,
                    operation_id=operation_id,
                    idempotency_key=idempotency_key,
                    expected_cloud_f_epoch=expected_cloud_f_epoch,
                    lease_expires_at=lease_expires_at,
                )
        except ServiceError as e:
            raise_mcp_error(e)

    @mcp.tool()
    async def list_historical_pointers(
        source_project_slug: Annotated[str, Field(min_length=1, max_length=63)],
        source_kind: Annotated[
            str | None, Field(pattern=r"^(decision|handoff|learning)$")
        ] = None,
        source_ref: Annotated[str | None, Field(max_length=512)] = None,
    ) -> list[dict[str, Any]]:
        """Read additive lineage records for one known project."""
        try:
            async with acquire_db() as db:
                ctx = current_mcp_context()
                visible_projects = await current_visible_projects(db, ctx)
                await require_unambiguous_visible_project(
                    db, ctx, source_project_slug, visible_projects
                )
                return await decision_service.list_historical_pointers(
                    db,
                    workspace_id=ctx.workspace_id,
                    source_project_slug=source_project_slug,
                    source_kind=source_kind,
                    source_ref=source_ref,
                )
        except ServiceError as e:
            raise_mcp_error(e)

    @mcp.tool()
    async def get_decision_operation(
        project_slug: Annotated[str, Field(min_length=1, max_length=63)],
        operation_id: Annotated[str, Field(min_length=1, max_length=128)],
    ) -> dict[str, Any]:
        """Read the persisted decision saga before any timeout retry."""
        try:
            async with acquire_db() as db:
                ctx = current_mcp_context()
                visible_projects = await current_visible_projects(db, ctx)
                await require_unambiguous_visible_project(
                    db, ctx, project_slug, visible_projects
                )
                result = await decision_service.read_decision_operation(
                    db,
                    workspace_id=ctx.workspace_id,
                    operation_id=operation_id,
                )
                if result["primary_project_slug"] != project_slug:
                    raise ServiceError(
                        code="decision_operation_scope_mismatch",
                        message="Decision operation does not belong to this project",
                    )
                return result
        except ServiceError as e:
            raise_mcp_error(e)
