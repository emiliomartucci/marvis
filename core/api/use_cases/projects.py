# v1.0.0 - 2026-05-27 - S1 F1.5: projects use_cases extracted from router (CENTRAL CRUD router)
"""Projects use_cases — pure domain logic, transport-agnostic (no ``fastapi``).

Sibling of :mod:`core.api.use_cases.learnings` (the S1 "collapse runtime"
TEMPLATE). One pure async function per operation, signature
``(ctx: CallerContext, db, *typed_args) -> <DTO>``. The HTTP router becomes a thin
adapter that resolves identity into a :class:`CallerContext`, calls these
functions, and maps :class:`ServiceError` -> ``HTTPException`` via
``routers/_adapter.to_http``. The Python MCP surface (later) calls the SAME
functions with ``CallerContext.local_single_user()``. One implementation, no fork.

``projects`` is a CENTRAL router: ~12 modules import its filesystem/discovery/git
helpers (``PROJECT_DIRS``, ``_find_project_entry``, ``_get_programs``,
``_parse_handoffs``, ``_set_project_dirs``, the project-index globals, …) directly
from ``core.api.routers.projects``. Those helpers are INFRASTRUCTURE (mutable
module globals, ``os.getuid()``-dependent git command, mtime caches), not CRUD
domain logic, so they STAY in the router and remain importable from there. This
module reaches for them FUNCTION-LOCALLY (exactly the ``search`` use_case pattern):
importing them at module top would pull in ``routers.projects`` -> ``fastapi`` and
break the ``use_cases-no-fastapi`` import-linter contract. Importing them lazily
keeps THIS module fastapi-free at import time (asserted by the smoke test + lint).

How the three TEMPLATE decisions land on the projects domain:

DECISION 1 — Visibility resolution at the adapter, enforcement in the use_case.
    Every slug-scoped read (``get_project`` / ``get_session_brief`` / ``handoffs``
    / ``status-updates`` / ``plans`` / ``git/*``) was guarded by
    ``visibility.check_project_access``, which raises a **404** (does NOT reveal
    existence). ``check_project_access`` needs ``UserInfo.teams``/``user_id`` —
    fields NOT carried by ``CallerContext`` by design — so the ADAPTER resolves
    ``visible_projects`` (via ``get_visible_projects``) and passes it in; this
    use_case only ENFORCES it, raising :class:`NotFoundError` (404) — NOT
    ``AuthorizationError`` — matching ``check_project_access`` exactly.
    ``visible_projects=None`` means "no restriction" (admin/agent, or MCP/local).
    ``list_programs`` keeps its own inline filter semantics: the adapter passes
    ``visible_projects`` and the use_case skips non-visible slugs (no raise — it is
    an aggregate listing, not a slug fetch).

DECISION 2 — ``deep`` KG enrichment is a per-surface adapter concern.
    ``get_project`` exposes ``?deep`` which calls ``check_deep_rate_limit`` /
    ``log_kg_deep_access`` (transport concerns) + ``build_kg_context_for_project``.
    The use_case returns the core ``ProjectDetail`` with ``kg_context=None``; the
    adapter, when ``deep`` is effective, performs rate-limit + log + attaches
    ``kg_context``. Behavior is identical to today. ``get_session_brief`` keeps its
    (always-on, non-rate-limited) ``kg_context`` assembled INSIDE the use_case —
    it is part of the bundle contract, not an opt-in enrichment.

DECISION 3 — input-normalization 400s stay in the adapter.
    The path-param ``pattern=`` validation (FastAPI 422) and the ``deep`` default
    resolution (``settings.kg_http_deep_default``) are transport concerns and stay
    in the router. The "no git repository" **400** (``project_type has no git
    repository``) IS a domain outcome, so it becomes a :class:`ValidationError`
    here (``http_status = 422``)? No — see the git use_cases below: the original
    raised ``HTTPException(400)`` and there is no domain ``ServiceError`` mapping to
    400, so the git "no-repo" branch stays an adapter concern too (the use_case
    returns the resolved repo path / a ``NotFoundError`` for a missing project, and
    the adapter raises the 400 for the present-but-non-git case). This preserves
    the exact 404-vs-400 split the original router had.

The response DTOs (``ProjectInfo`` / ``ProjectDetail`` / ``ProgramInfo`` /
``ProjectCreateResponse`` / ``HandoffEntry`` / ``DocEntry`` /
``StatusUpdateFeedItem`` / ``StatusUpdateFeedResponse``) live in
``core.api.models`` and are NOT moved (same as the costs use_case). The REQUEST
models (``ProjectCreateRequest`` / ``StatusUpdateFeedCreateRequest``) stay in the
router's import surface, parsed by FastAPI before the use_case is called.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path

import aiosqlite

from core.api.models import (
    DocEntry,
    HandoffEntry,
    ProgramInfo,
    ProjectCreateResponse,
    ProjectDetail,
    ProjectInfo,
    StatusCounts,
    StatusUpdateFeedItem,
    StatusUpdateFeedResponse,
)
from core.api.use_cases._context import CallerContext, require_role_ctx
from core.api.use_cases._errors import ConflictError, NotFoundError, ServiceError

logger = logging.getLogger(__name__)

# Default data dir for new work projects (parity with the original router constant).
_DATA_PROJECT_DIR = Path("/data/projects")


# ---------------------------------------------------------------------------
# Visibility enforcement (DECISION 1)
# ---------------------------------------------------------------------------


def _enforce_project_access(slug: str, visible_projects: set[str] | None) -> None:
    """Enforce visibility for a slug-scoped endpoint (DECISION 1).

    Mirrors ``visibility.check_project_access``: ``visible_projects=None`` means
    unrestricted (admin/agent, or MCP/local); otherwise a slug outside the set
    raises a **404** (does NOT reveal existence — same as today).
    """
    if visible_projects is not None and slug not in visible_projects:
        raise NotFoundError(code="project_not_found", message="Not found")


# ---------------------------------------------------------------------------
# Use cases
# ---------------------------------------------------------------------------


async def list_programs(
    ctx: CallerContext,
    db: aiosqlite.Connection,
    *,
    visible_projects: set[str] | None = None,
) -> list[ProgramInfo]:
    """List all programs with their projects and per-project task counts.

    Aggregate listing (any authenticated caller). Visibility (DECISION 1) is an
    inline FILTER here, not a raise: the adapter resolves ``visible_projects`` and
    this use_case skips slugs not in the set (``None`` => unrestricted). Reaches
    the filesystem helpers (project index, programs loader, metadata reader) and
    the git command constant function-locally to stay fastapi-free at import.
    """
    from core.api.routers.projects import (  # function-local: keeps this module fastapi-free
        _INDEX_TTL,
        _build_project_index,
        _get_all_task_counts,
        _get_latest_status_updates,
        _get_programs,
        _get_project_metadata,
    )
    import core.api.routers.projects as _projects_mod

    programs = _get_programs()
    if not _projects_mod._project_index or (
        time.monotonic() - _projects_mod._index_built_at > _INDEX_TTL
    ):
        _build_project_index()
    project_index = _projects_mod._project_index

    visible_slugs = visible_projects  # already resolved by the adapter (DECISION 1)

    task_counts = await _get_all_task_counts(db)
    status_updates = await _get_latest_status_updates(db)

    result: list[ProgramInfo] = []
    seen_slugs: set[str] = set()

    for prog_name, prog_data in programs.items():
        if not isinstance(prog_data, dict):
            continue
        description = prog_data.get("description", "")
        project_slugs = prog_data.get("projects", [])
        projects: list[ProjectInfo] = []
        for slug in project_slugs:
            seen_slugs.add(slug)
            if visible_slugs is not None and slug not in visible_slugs:
                continue
            entry = project_index.get(slug)
            on_server = entry is not None
            meta: dict = {}
            if on_server and entry:
                meta = _get_project_metadata(entry.metadata_path)

            counts = task_counts.get(slug, {})
            sc = StatusCounts(**{k: v for k, v in counts.items() if hasattr(StatusCounts, k)})

            su = status_updates.get(slug)
            proj_status = su[0] if su else None
            last_su_date = su[1] if su else None

            working_path = None
            if entry:
                working_path = str(entry.metadata_path.resolve())

            projects.append(ProjectInfo(
                slug=slug,
                name=slug,
                program=prog_name,
                language=meta.get("language"),
                lifecycle=meta.get("lifecycle"),
                phase=meta.get("phase"),
                scope=meta.get("scope"),
                description=meta.get("description"),
                type=entry.project_type if entry else None,
                repo_path=str(entry.repo_path) if entry and entry.repo_path else None,
                metadata_path=str(entry.metadata_path.resolve()) if entry else None,
                status=proj_status,
                task_counts=sc,
                last_status_update=last_su_date,
                on_server=on_server,
                path=working_path,
            ))
        result.append(ProgramInfo(name=prog_name, description=description, projects=projects))

    # Standalone projects (in index but not in any program)
    standalone: list[ProjectInfo] = []
    for slug, entry in project_index.items():
        if slug in seen_slugs:
            continue
        if visible_slugs is not None and slug not in visible_slugs:
            continue
        meta = _get_project_metadata(entry.metadata_path)
        counts = task_counts.get(slug, {})
        sc = StatusCounts(**{k: v for k, v in counts.items() if hasattr(StatusCounts, k)})
        su = status_updates.get(slug)
        working_path = str(entry.metadata_path.resolve())
        standalone.append(ProjectInfo(
            slug=slug,
            name=slug,
            program=None,
            language=meta.get("language"),
            lifecycle=meta.get("lifecycle"),
            phase=meta.get("phase"),
            scope=meta.get("scope"),
            description=meta.get("description"),
            type=entry.project_type,
            repo_path=str(entry.repo_path) if entry.repo_path else None,
            metadata_path=str(entry.metadata_path.resolve()),
            status=su[0] if su else None,
            task_counts=sc,
            last_status_update=su[1] if su else None,
            on_server=True,
            path=working_path,
        ))
    if standalone:
        result.append(ProgramInfo(name="standalone", description="Standalone projects", projects=standalone))

    return result


async def create_project(
    ctx: CallerContext,
    db: aiosqlite.Connection,
    *,
    slug: str,
    name: str | None,
    program: str | None,
    scope: str | None,
    description: str | None,
    lifecycle: str,
    language: str | None,
    type: str,
) -> ProjectCreateResponse:
    """Create a new work project on the server (operator+).

    Creates the directory structure, project.yaml, context.md, and .task file
    under ``/data/projects/{slug}/``. Raises :class:`ConflictError` (409) if the
    slug already exists (in index or on disk) and :class:`ServiceError` (500) on a
    filesystem error — parity with the original ``HTTPException(409)`` /
    ``HTTPException(500)``. The directory write itself lives in the router helper
    (filesystem side effect) so this module stays free of ``os``/``shutil``/``yaml``
    coupling and the test seam ``api.routers.projects._find_project_entry`` keeps
    working.
    """
    require_role_ctx(ctx, "operator", "admin", "super_admin")

    from core.api.routers.projects import _create_project_on_disk, _find_project_entry

    project_dir = _DATA_PROJECT_DIR / slug

    if _find_project_entry(slug) is not None:
        raise ConflictError(code="project_exists", message=f"Project '{slug}' already exists")
    if project_dir.exists():
        raise ConflictError(
            code="project_dir_exists",
            message=f"Project directory already exists: {project_dir}",
        )
    if not _DATA_PROJECT_DIR.is_dir():
        raise ServiceError(
            code="data_dir_missing",
            message=f"Data directory does not exist: {_DATA_PROJECT_DIR}",
        )

    display_name = name or slug
    metadata_path = _create_project_on_disk(
        slug=slug,
        display_name=display_name,
        program=program,
        scope=scope,
        description=description,
        lifecycle=lifecycle,
        language=language,
        type=type,
    )

    logger.info("Project created: %s by %s", slug, ctx.username)

    return ProjectCreateResponse(
        slug=slug,
        name=display_name,
        program=program,
        language=language,
        lifecycle=lifecycle,
        description=description,
        type=type,
        scope=scope,
        metadata_path=metadata_path,
    )


async def get_project(
    ctx: CallerContext,
    db: aiosqlite.Connection,
    *,
    slug: str,
    visible_projects: set[str] | None = None,
) -> ProjectDetail:
    """Get project detail with context.md, config, handoffs, plans, solutions.

    Visibility enforced (DECISION 1, 404). Returns ``kg_context=None``; the adapter
    attaches KG context + does rate-limit/log when ``deep`` is effective (D2).
    """
    _enforce_project_access(slug, visible_projects)

    from core.api.routers.projects import (
        _find_project_entry,
        _get_programs,
        _get_project_metadata,
        _parse_context_config,
        _parse_docs,
        _parse_handoffs,
        _safe_read_file,
    )

    entry = _find_project_entry(slug)
    if not entry:
        raise NotFoundError(code="project_not_found", message="Project not found on server")
    path = entry.metadata_path.resolve()

    context_md = _safe_read_file(path, "context.md")
    meta = _get_project_metadata(path)
    config = _parse_context_config(context_md) if context_md else {}

    program = meta.get("program")
    if not program:
        programs = _get_programs()
        for prog_name, prog_data in programs.items():
            if isinstance(prog_data, dict) and slug in prog_data.get("projects", []):
                program = prog_name
                break

    deploy = meta.get("deploy") if isinstance(meta.get("deploy"), dict) else None

    return ProjectDetail(
        slug=slug,
        name=slug,
        program=program,
        language=meta.get("language"),
        lifecycle=meta.get("lifecycle"),
        phase=meta.get("phase"),
        scope=meta.get("scope"),
        description=meta.get("description"),
        type=entry.project_type,
        repo_path=str(entry.repo_path) if entry.repo_path else None,
        metadata_path=str(path),
        context_md=context_md,
        config=config,
        deploy=deploy,
        handoffs=_parse_handoffs(path),
        plans=_parse_docs(path, "plans"),
        solutions=_parse_docs(path, "solutions"),
        kg_context=None,  # adapter attaches on effective deep=true (DECISION 2)
    )


async def get_session_brief(
    ctx: CallerContext,
    db: aiosqlite.Connection,
    *,
    slug: str,
    deep: bool = False,
    visible_projects: set[str] | None = None,
) -> dict:
    """Pre-assembled context bundle for agent cold-start.

    Visibility enforced (DECISION 1, 404). Unlike ``get_project``, the
    ``kg_context`` here is part of the bundle contract (always assembled, not
    rate-limited), so it is built INSIDE the use_case (D2 note). ``deep`` only
    widens the per-bucket KG limits.
    """
    _enforce_project_access(slug, visible_projects)

    import json
    import re

    import yaml

    from core.api.config import settings
    from core.api.routers.projects import _find_project_entry, _get_programs, _get_project_metadata, _safe_read_file
    from core.api.services.kg.lens import build_kg_context_for_project

    entry = _find_project_entry(slug)
    if not entry:
        raise NotFoundError(code="project_not_found", message="Project not found on server")
    path = entry.metadata_path.resolve()

    meta = _get_project_metadata(path)
    workspace_id = ctx.workspace_id or "ws_default"

    identity = (
        "Tu sei Marvis, AI assistant per project management e development. "
        "Personalita: opinioni forti, umorismo naturale, garbo nel segnalare scelte sbagliate."
    )

    program = meta.get("program")
    if not program:
        programs = _get_programs()
        for prog_name, prog_data in programs.items():
            if isinstance(prog_data, dict) and slug in prog_data.get("projects", []):
                program = prog_name
                break

    project_info = {
        "slug": slug,
        "name": meta.get("description") or slug,
        "lifecycle": meta.get("lifecycle"),
        "type": entry.project_type,
        "phase": meta.get("phase"),
        "language": meta.get("language"),
        "program": program,
        "repo_path": str(entry.repo_path) if entry.repo_path else None,
        "metadata_path": str(path),
    }

    cur = await db.execute(
        "SELECT id, title, status, priority, tags "
        "FROM tasks "
        "WHERE project = ? AND status IN ('in_progress', 'approved') AND deleted_at IS NULL "
        "AND COALESCE(workspace_id, 'ws_default') = ? "
        "ORDER BY CASE status WHEN 'in_progress' THEN 0 ELSE 1 END, "
        "CASE priority WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END "
        "LIMIT 20",
        [slug, workspace_id],
    )
    open_tasks = []
    async for row in cur:
        tags_raw = row["tags"] or "[]"
        try:
            tags = json.loads(tags_raw)
        except (ValueError, TypeError):
            tags = []
        open_tasks.append({
            "id": row["id"],
            "title": row["title"],
            "status": row["status"],
            "priority": row["priority"],
            "tags": tags,
        })

    latest_handoff = None
    memory_dir = path / "memory"
    if memory_dir.is_dir():
        handoff_files = sorted(memory_dir.glob("handoff-*.md"), reverse=True)
        if handoff_files:
            f = handoff_files[0]
            content = _safe_read_file(path, f"memory/{f.name}")
            if content:
                date_match = re.search(r"handoff-(\d{4}-\d{2}-\d{2})", f.name)
                ho_date = date_match.group(1) if date_match else ""
                if content.startswith("---"):
                    end = content.find("---", 3)
                    if end > 0:
                        try:
                            fm = yaml.safe_load(content[3:end])
                            if isinstance(fm, dict) and "date" in fm:
                                ho_date = str(fm["date"])
                        except Exception:
                            pass
                latest_handoff = {
                    "title": f.stem.replace("-", " ").strip(),
                    "date": ho_date,
                    "summary_first_500_chars": content[:500],
                }

    cur_l = await db.execute(
        "SELECT id, title, category, severity "
        "FROM learnings "
        "WHERE (project = ? OR project IS NULL) AND COALESCE(workspace_id, 'ws_default') = ? "
        "ORDER BY last_occurrence DESC NULLS LAST "
        "LIMIT 5",
        [slug, workspace_id],
    )
    recent_learnings = []
    async for row in cur_l:
        recent_learnings.append({
            "id": row["id"],
            "title": row["title"],
            "category": row["category"],
            "severity": row["severity"],
        })

    top_salience_docs = []
    try:
        cur_d = await db.execute(
            "SELECT id, doc_title, doc_type, salience "
            "FROM documents "
            "WHERE COALESCE(workspace_id, 'ws_default') = ? AND archived = 0 AND project = ? "
            "ORDER BY salience DESC "
            "LIMIT 5",
            [workspace_id, slug],
        )
        async for row in cur_d:
            top_salience_docs.append({
                "title": row["doc_title"],
                "doc_type": row["doc_type"],
                "salience": row["salience"] if row["salience"] is not None else 0.5,
            })
    except Exception:
        # documents table may not have salience column in all environments
        pass

    cur_s = await db.execute(
        "SELECT status, COUNT(*) as cnt "
        "FROM tasks "
        "WHERE project = ? AND deleted_at IS NULL AND COALESCE(workspace_id, 'ws_default') = ? "
        "GROUP BY status",
        [slug, workspace_id],
    )
    status_counts: dict[str, int] = {}
    async for row in cur_s:
        status_counts[row["status"]] = row["cnt"]

    stats = {
        "in_progress_count": status_counts.get("in_progress", 0),
        "approved_count": status_counts.get("approved", 0),
        "pending_count": status_counts.get("pending", 0),
        "completed_count": status_counts.get("completed", 0),
        "wip_limit": settings.wip_max_in_progress,
    }

    kg_context = await build_kg_context_for_project(db, slug, deep=deep)

    brief = {
        "identity": identity,
        "project": project_info,
        "open_tasks": open_tasks,
        "latest_handoff": latest_handoff,
        "recent_learnings": recent_learnings,
        "top_salience_docs": top_salience_docs,
        "stats": stats,
        "kg_context": kg_context,
    }

    # Track 2 #2 SEAM (flag MARVIS_BRIEF_CITATIONS, DEFAULT OFF).
    # When off this is a strict no-op: `brief` is returned byte-for-byte unchanged,
    # nothing from the grounding layer is imported, no model is touched. The on-path
    # (select->generate->verify the synthesized prose against #4 chunk spans) is
    # wired deliberately, off-host, together with an NLI head — intentionally NOT
    # activated here.
    if settings.brief_citations_enabled:  # pragma: no cover - off by default
        from core.api.services.grounding import annotate_brief_grounding

        brief = await annotate_brief_grounding(db, slug, brief)

    return brief


async def get_handoffs(
    ctx: CallerContext,
    db: aiosqlite.Connection,
    *,
    slug: str,
    visible_projects: set[str] | None = None,
) -> list[HandoffEntry]:
    """List handoff files for a project (visibility enforced, 404)."""
    _enforce_project_access(slug, visible_projects)

    from core.api.routers.projects import _find_project_path, _parse_handoffs

    path = _find_project_path(slug)
    if not path:
        raise NotFoundError(code="project_not_found", message="Project not found on server")
    return _parse_handoffs(path)


async def list_status_updates_feed(
    ctx: CallerContext,
    db: aiosqlite.Connection,
    *,
    slug: str,
    limit: int = 20,
    visible_projects: set[str] | None = None,
) -> StatusUpdateFeedResponse:
    """Feed-style status updates (persisted + derived from handoffs/commits).

    Visibility enforced (DECISION 1, 404). Returns an empty feed for a project not
    present in the index (parity: the original returned the service result, which
    yields no derived entries when paths are ``None``).
    """
    _enforce_project_access(slug, visible_projects)

    from core.api.routers.projects import _find_project_entry
    from core.api.services import project_status_updates as status_feed_service

    entry = _find_project_entry(slug)
    metadata_path = entry.metadata_path if entry else None
    repo_path = entry.repo_path if entry else None
    updates, total = await status_feed_service.list_feed(
        db,
        slug,
        metadata_path=metadata_path,
        repo_path=repo_path,
        limit=limit,
    )
    return StatusUpdateFeedResponse(updates=updates, total=total)


async def create_status_update_feed(
    ctx: CallerContext,
    db: aiosqlite.Connection,
    *,
    slug: str,
    content_md: str,
    author_display: str | None = None,
    visible_projects: set[str] | None = None,
) -> StatusUpdateFeedItem:
    """Create a manual feed entry for a project (operator+, visibility enforced)."""
    require_role_ctx(ctx, "operator", "admin", "super_admin")
    _enforce_project_access(slug, visible_projects)

    from core.api.services import project_status_updates as status_feed_service

    author_display_final = author_display or ctx.username
    return await status_feed_service.create_manual_update(
        db,
        slug=slug,
        content_md=content_md,
        author=ctx.username,
        author_display=author_display_final,
    )


async def get_plans(
    ctx: CallerContext,
    db: aiosqlite.Connection,
    *,
    slug: str,
    visible_projects: set[str] | None = None,
) -> list[DocEntry]:
    """List all docs for a project (iterates docs/ subdirs). Visibility enforced (404)."""
    _enforce_project_access(slug, visible_projects)

    from core.api.routers.projects import _find_project_path, _parse_docs

    path = _find_project_path(slug)
    if not path:
        raise NotFoundError(code="project_not_found", message="Project not found on server")
    docs_dir = path / "docs"
    if not docs_dir.is_dir():
        return []
    results: list[DocEntry] = []
    for subdir in sorted(docs_dir.iterdir()):
        if subdir.is_dir() and not subdir.name.startswith("."):
            try:
                results.extend(_parse_docs(path, subdir.name))
            except Exception:
                # Defensive: never fail the whole listing for a single bad subdir
                continue
        if len(results) >= 500:  # safety cap DoS
            break
    return results[:500]


async def resolve_git_repo(
    ctx: CallerContext,
    db: aiosqlite.Connection,
    *,
    slug: str,
    visible_projects: set[str] | None = None,
) -> Path:
    """Resolve a slug to its git repo path for the ``git/*`` endpoints.

    Visibility enforced (DECISION 1, 404). Returns the repo ``Path`` on success.
    A non-existent project raises :class:`NotFoundError` (404). A project that
    exists but has no repo raises :class:`NoGitRepoError` (``http_status = 400``,
    carrying ``project_type``) so ``to_http`` maps it to the EXACT
    ``400 "Project type '<t>' has no git repository"`` the original router raised
    — preserving the 404-vs-400 split per endpoint.
    """
    _enforce_project_access(slug, visible_projects)

    from core.api.routers.projects import _find_git_path, _find_project_entry

    repo = _find_git_path(slug)
    if repo:
        return repo
    entry = _find_project_entry(slug)
    if not entry:
        raise NotFoundError(code="project_not_found", message="Project not found on server")
    raise NoGitRepoError(
        code="no_git_repository",
        message=f"Project type '{entry.project_type}' has no git repository",
        project_type=entry.project_type,
    )


class NoGitRepoError(ServiceError):
    """Project exists but has no git repository.

    Carries ``http_status = 400`` so ``to_http`` maps it to the EXACT 400 the
    original router raised (``"Project type '<t>' has no git repository"``). Kept
    as a domain error (not an adapter-only HTTPException) so the MCP surface can
    react to it too; the 400 is just the HTTP hint.
    """

    http_status = 400

    def __init__(self, *, code: str, message: str, project_type: str) -> None:
        super().__init__(code=code, message=message)
        self.project_type = project_type
