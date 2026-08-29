# v1.0.0 - 2026-06-01 - B1: brain-aware workflow MCP tool group (framework + plan template)
"""Workflow MCP tools — agent-agnostic, brain-aware playbooks (B1 framework).

A workflow tool does NOT orchestrate. It RETURNS a playbook (markdown text — not
the MCP ``prompts`` primitive, whose client support is uneven) that the host agent
then executes. The MCP stays light: the host agent is the compute engine, no extra
keys, no duplicated LLM. Each playbook is the same 3-beat skeleton — CONSULT the
brain → DO the work (capability-tiered, no Claude-Task assumption) → SAVE back via
a persistence callback that writes the artifact in the right folder AND embeds it
so the next session finds it by meaning (the compounding loop, B1 depends on A1).

Testability seam: the real logic lives in module-level async helpers
(:func:`_build_plan_playbook`, :func:`_save_plan_artifact`); the ``@mcp.tool()``
``plan`` / ``save_plan`` are thin wrappers. Tests import and call the helpers
WITHOUT importing the MCP SDK (``mcp.server.fastmcp`` is not a runtime dep of this
module — parity with ``server.py`` / ``_adapter.py``).

fastapi-free invariant: this module imports no ``fastapi``. The project-path
resolver (``routers.projects._find_project_path``, whose module pulls fastapi) and
the use_cases are imported FUNCTION-LOCALLY, exactly as ``use_cases.search`` does
to stay fastapi-free at import time.

B2 adds ``brainstorm`` / ``compound`` by dropping ``workflow_playbooks/brainstorm.md``
etc. and copying the helper+wrapper pair below. The three ``save_*`` callbacks share
one private writer (:func:`_save_doc_artifact`, parametrized by subdir + suffix) so the
folder/dating/kebab/embed logic lives in exactly one place — no copy-paste drift.
"""
from __future__ import annotations

import datetime as _dt
import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Annotated, Any

from pydantic import Field

from core.api.mcp._adapter import (
    LOCAL_CTX,
    acquire_db,
    acquire_write_db,
    current_mcp_context,
    current_visible_projects,
    require_unambiguous_visible_project,
    raise_mcp_error,
)
from core.api.use_cases._context import CallerContext, require_workspace_ctx
from core.api.use_cases._errors import NotFoundError, ServiceError
from core.api.services import project_lifecycle

logger = logging.getLogger(__name__)

#: Playbooks ship bundled with the package (versioned with the CLI), one markdown
#: file per workflow. B2 drops more files here without touching the loader. The dir
#: is named ``workflow_playbooks`` (not ``workflows``) to avoid colliding with this
#: ``workflows.py`` module name on the import path.
_PLAYBOOKS_DIR = Path(__file__).parent / "workflow_playbooks"

#: Kebab-case slug for the dated plan filename (mirrors the conventional-commit
#: title rule: strip the ``type:`` prefix, lowercase, collapse to ``a-z0-9-``).
_KEBAB_STRIP_RE = re.compile(r"[^a-z0-9]+")


async def _current_workflow_scope() -> tuple[CallerContext, set[str] | None]:
    """Resolve one authenticated workspace and project allowlist per tool call."""
    ctx = current_mcp_context()
    require_workspace_ctx(ctx)
    if ctx is LOCAL_CTX:
        return ctx, None
    async with acquire_db() as db:
        visible_projects = await current_visible_projects(db, ctx)
        if visible_projects is None:
            raise NotFoundError(code="project_not_found", message="Project not found")
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
        return ctx, safe


def _require_visible_project(
    project: str | None,
    visible_projects: set[str] | None,
) -> None:
    if (
        project is not None
        and visible_projects is not None
        and project not in visible_projects
    ):
        raise NotFoundError(code="project_not_found", message="Project not found")


def _load_playbook(name: str) -> str:
    """Read a bundled playbook markdown file by stem (e.g. ``plan``).

    Raises ``FileNotFoundError`` if the file is missing — a packaging error, not a
    runtime condition, so it surfaces loudly rather than degrading to an empty
    playbook.
    """
    return (_PLAYBOOKS_DIR / f"{name}.md").read_text(encoding="utf-8")


def _kebab_title(title: str) -> str:
    """Conventional-title -> kebab slug: ``feat: Add Auth Flow`` -> ``add-auth-flow``."""
    # Drop a leading ``type:`` prefix (feat/fix/refactor/...) so the slug is the
    # descriptive part only.
    body = title.split(":", 1)[1] if ":" in title else title
    slug = _KEBAB_STRIP_RE.sub("-", body.lower()).strip("-")
    return slug or "doc"


async def _prefetch_brain_context(
    feature: str,
    project: str | None,
    *,
    ctx: CallerContext = LOCAL_CTX,
    visible_projects: set[str] | None = None,
) -> str:
    """Pull related prior work + applicable learnings, best-effort, fastapi-free.

    Calls the SAME use_cases the host agent is told to call, so the returned
    playbook already starts grounded instead of merely instructing a lookup. Pure
    best-effort: if search is down (embedding backend 503), the embedder is absent, or anything
    else raises, return a fallback line that tells the agent to run the consult
    itself — the playbook must still be runnable on a brain that is momentarily
    unavailable.

    Returns a markdown block (the ``{brain_context}`` substitution) — never raises.
    """
    require_workspace_ctx(ctx)
    _require_visible_project(project, visible_projects)
    try:
        from core.api.use_cases import learnings as learnings_uc
        from core.api.use_cases import search as search_uc

        # search opens its own connections from settings (no request db); learnings
        # needs a read connection.
        search_res = await search_uc.search(ctx, q=feature)
        async with acquire_db() as db:
            learn_res = await learnings_uc.check_learnings(
                ctx,
                db,
                query=feature,
                visible_projects=visible_projects,
            )
    except Exception:
        logger.debug("plan prefetch unavailable (non-critical)", exc_info=True)
        return (
            "_Brain prefetch unavailable — run the consult yourself: call "
            "`mcp__marvis__search` + `mcp__marvis__check_learnings` before drafting._"
        )

    return _format_brain_context(
        search_res,
        learn_res,
        visible_projects=visible_projects,
    )


def _format_brain_context(
    search_res: Any,
    learn_res: Any,
    visible_projects: set[str] | None = None,
) -> str:
    """Render the prefetched search + learnings DTOs into a compact markdown block.

    Pulls the highest-signal hits (related plans/tasks/files + applicable learnings)
    so the agent reads a summary, not raw JSON. Tolerant of plain dicts (test
    fakes) and the Pydantic DTOs (live use_cases) alike.
    """

    def _get(obj: Any, key: str, default: Any = None) -> Any:
        if isinstance(obj, dict):
            return obj.get(key, default)
        return getattr(obj, key, default)

    lines: list[str] = []

    # Related prior work: merge the search buckets, keep the top few by score.
    hits: list[tuple[float, str, str]] = []  # (score, doc_type, title)
    for bucket in ("plans", "files", "tasks", "handoffs", "projects", "learnings"):
        for hit in _get(search_res, bucket, []) or []:
            title = _get(hit, "title", "")
            score = _get(hit, "score", 0.0) or 0.0
            doc_type = _get(hit, "doc_type", bucket.rstrip("s"))
            project = _get(hit, "project", None)
            if visible_projects is not None:
                project_bound = bucket in {
                    "plans",
                    "files",
                    "tasks",
                    "handoffs",
                    "projects",
                }
                if (project_bound and not project) or (
                    project and project not in visible_projects
                ):
                    continue
            if title:
                hits.append((float(score), str(doc_type), str(title)))
    hits.sort(key=lambda h: h[0], reverse=True)
    if hits:
        lines.append("**Related prior work** (top hits, read before deciding):")
        for score, doc_type, title in hits[:6]:
            lines.append(f"- [{doc_type}] {title} (score {score:.2f})")
    else:
        lines.append("**Related prior work:** none surfaced by semantic search.")

    # Applicable learnings = hard constraints.
    learnings = _get(learn_res, "results", []) or []
    if learnings:
        lines.append("")
        lines.append("**Applicable learnings** (treat as hard constraints):")
        for lrn in learnings[:6]:
            title = _get(lrn, "title", "")
            severity = _get(lrn, "severity", "")
            if title:
                tag = f" [{severity}]" if severity else ""
                lines.append(f"- {title}{tag}")
    else:
        lines.append("")
        lines.append("**Applicable learnings:** none matched.")

    return "\n".join(lines)


async def _build_plan_playbook(
    feature: str,
    project: str | None = None,
    *,
    ctx: CallerContext | None = None,
    visible_projects: set[str] | None = None,
) -> str:
    """Build the plan playbook text, with the brain prefetch spliced into CONSULT.

    The bundled ``plan.md`` carries the 3-beat skeleton with ``{brain_context}`` and
    ``{feature}`` placeholders; this fills them. The prefetch is best-effort
    (degrades to a self-consult instruction) so the returned playbook is always
    runnable. Module-level + SDK-free so tests call it directly.
    """
    template = _load_playbook("plan")
    if ctx is None:
        brain_context = await _prefetch_brain_context(feature, project)
    else:
        brain_context = await _prefetch_brain_context(
            feature,
            project,
            ctx=ctx,
            visible_projects=visible_projects,
        )
    return template.replace("{brain_context}", brain_context).replace(
        "{feature}", feature
    )


async def _build_brainstorm_playbook(
    topic: str,
    project: str | None = None,
    *,
    ctx: CallerContext | None = None,
    visible_projects: set[str] | None = None,
) -> str:
    """Build the brainstorm playbook text, brain prefetch spliced into CONSULT.

    Twin of :func:`_build_plan_playbook` — same prefetch (best-effort, degrades to a
    self-consult line) so the returned playbook always runs. The bundled
    ``brainstorm.md`` carries ``{brain_context}`` + ``{topic}`` placeholders; this
    fills them. Module-level + SDK-free so tests call it directly.
    """
    template = _load_playbook("brainstorm")
    if ctx is None:
        brain_context = await _prefetch_brain_context(topic, project)
    else:
        brain_context = await _prefetch_brain_context(
            topic,
            project,
            ctx=ctx,
            visible_projects=visible_projects,
        )
    return template.replace("{brain_context}", brain_context).replace(
        "{topic}", topic
    )


async def _build_compound_playbook(
    what: str,
    project: str | None = None,
    *,
    ctx: CallerContext | None = None,
    visible_projects: set[str] | None = None,
) -> str:
    """Build the compound playbook text, brain prefetch spliced into CONSULT.

    Twin of :func:`_build_plan_playbook`. The bundled ``compound.md`` carries
    ``{brain_context}`` + ``{what}`` placeholders; this fills them. The prefetch is
    best-effort (degrades to a self-consult instruction). Module-level + SDK-free so
    tests call it directly.
    """
    template = _load_playbook("compound")
    if ctx is None:
        brain_context = await _prefetch_brain_context(what, project)
    else:
        brain_context = await _prefetch_brain_context(
            what,
            project,
            ctx=ctx,
            visible_projects=visible_projects,
        )
    return template.replace("{brain_context}", brain_context).replace("{what}", what)


def _resolve_project_path(project: str) -> Path | None:
    """Resolve a project slug to its metadata dir, fastapi-free.

    Reuses the SAME resolver ``use_cases.search.reindex_project`` uses
    (``routers.projects._find_project_path``), imported function-locally so this
    module never pulls fastapi at import time. Returns ``None`` for an unknown slug.
    """
    from core.api.routers.projects import _find_project_path

    return _find_project_path(project)


def _atomic_write_text(path: Path, body: str) -> None:
    descriptor, temporary = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_CLOEXEC)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


async def _save_doc_artifact(
    project: str,
    title: str,
    body: str,
    *,
    subdir: str,
    suffix: str,
    ctx: CallerContext = LOCAL_CTX,
    visible_projects: set[str] | None = None,
) -> dict[str, Any]:
    """Write a workflow artifact under ``<project>/docs/<subdir>/`` + index/embed best-effort.

    The ONE save body the three workflow callbacks share, parametrized by
    ``subdir`` (``plans`` / ``brainstorms`` / ``solutions``) and ``suffix``
    (``-plan`` / ``-brainstorm`` / ``""``). Owns two things the host agent must not
    hand-roll: (1) the correct dated/kebab filename + folder, (2) the embed so the
    just-written artifact is immediately findable by meaning (B2 ⇽ A1). The
    lexical index is attempted first so the artifact is BM25-recoverable even
    when the vector embedder is down. The embed is best-effort and runs OUTSIDE
    any writer lock (it is its own ``write_db`` acquisition inside
    ``embed_doc_document``): a failed embed NEVER fails the save. Returns
    ``{path, indexed, embedded}``.

    Filename: ``<YYYY-MM-DD>-<kebab-title>[<suffix>].md``. When ``suffix`` is
    non-empty it is appended unless the slug already ends in it (avoid a double
    ``-plan``/``-brainstorm``); an empty suffix means the dated kebab slug stands
    alone (solutions).
    """
    workspace_id = require_workspace_ctx(ctx)
    _require_visible_project(project, visible_projects)
    project_path = _resolve_project_path(project)
    if project_path is None:
        raise NotFoundError(
            code="project_not_found",
            message=f"Project '{project}' not found",
        )

    today = _dt.date.today().isoformat()
    slug = _kebab_title(title)
    if suffix and not slug.endswith(suffix):
        stem = f"{slug}{suffix}"
    else:
        stem = slug
    relative_path = f"docs/{subdir}/{today}-{stem}.md"
    docs_dir = project_path / "docs" / subdir
    doc_path = project_path / relative_path
    async with acquire_write_db(label=f"mcp.save_{subdir}") as db:
        async with project_lifecycle.guarded_project_file_write(
            ctx,
            db,
            project_slug=project,
            writer_kind=f"workflow_{subdir}",
            resource_ref=relative_path,
            projects_root=project_path.parent,
        ):
            docs_dir.mkdir(parents=True, exist_ok=True)
            _atomic_write_text(doc_path, body)

    indexed = False
    try:
        from core.api.services import embedding_service

        await embedding_service.index_doc_document_fts(
            file_path=str(doc_path),
            title=title,
            content=body,
            project=project,
            workspace_id=workspace_id,
        )
        indexed = True
    except Exception:
        logger.debug(
            "doc FTS index-on-save failed for %s (non-critical)",
            doc_path,
            exc_info=True,
        )

    embedded = False
    try:
        from core.api.services import embedding_service

        await embedding_service.embed_doc_document(
            file_path=str(doc_path),
            title=title,
            content=body,
            project=project,
            workspace_id=workspace_id,
        )
        # is_available() gates the embed; treat a no-op (unavailable) as not embedded
        # so the return reflects reality.
        embedded = embedding_service.is_available()
    except Exception:
        logger.debug(
            "doc embed-on-save failed for %s (non-critical)",
            doc_path,
            exc_info=True,
        )

    return {"path": str(doc_path), "indexed": indexed, "embedded": embedded}


async def _save_plan_artifact(
    project: str,
    title: str,
    body: str,
    *,
    ctx: CallerContext = LOCAL_CTX,
    visible_projects: set[str] | None = None,
) -> dict[str, Any]:
    """Persist a plan to ``<project>/docs/plans/<date>-<kebab>-plan.md`` (+ embed).

    Thin adapter over :func:`_save_doc_artifact` — the SAVE-beat persistence callback
    for the ``plan`` workflow. Returns ``{path, embedded}``.
    """
    return await _save_doc_artifact(
        project,
        title,
        body,
        subdir="plans",
        suffix="-plan",
        ctx=ctx,
        visible_projects=visible_projects,
    )


async def _save_brainstorm_artifact(
    project: str,
    title: str,
    body: str,
    *,
    ctx: CallerContext = LOCAL_CTX,
    visible_projects: set[str] | None = None,
) -> dict[str, Any]:
    """Persist a brainstorm to ``<project>/docs/brainstorms/<date>-<kebab>-brainstorm.md``.

    Thin adapter over :func:`_save_doc_artifact` — the SAVE-beat persistence callback
    for the ``brainstorm`` workflow. Returns ``{path, embedded}``.
    """
    return await _save_doc_artifact(
        project,
        title,
        body,
        subdir="brainstorms",
        suffix="-brainstorm",
        ctx=ctx,
        visible_projects=visible_projects,
    )


async def _save_compound_artifact(
    project: str,
    title: str,
    body: str,
    *,
    ctx: CallerContext = LOCAL_CTX,
    visible_projects: set[str] | None = None,
) -> dict[str, Any]:
    """Persist a solution doc to ``<project>/docs/solutions/<date>-<kebab>.md`` (+ embed).

    Thin adapter over :func:`_save_doc_artifact` — the SAVE-beat persistence callback
    for the ``compound`` workflow. NOTE: the solutions folder and NO ``-plan``/
    ``-brainstorm`` suffix (just the dated kebab slug). Returns ``{path, embedded}``.
    """
    return await _save_doc_artifact(
        project,
        title,
        body,
        subdir="solutions",
        suffix="",
        ctx=ctx,
        visible_projects=visible_projects,
    )


def register(mcp) -> None:
    """Register the workflows tool group on the shared FastMCP instance."""

    @mcp.tool()
    async def plan(
        feature: Annotated[str, Field(min_length=1, max_length=2000)],
        project: str | None = None,
    ) -> str:
        """Return a planning playbook (istruzioni) — NON pianifica e NON esegue nulla.

        QUANDO USARLO: stai per pianificare un lavoro (feature/bug/refactor) e vuoi un piano strutturato che parte dalla memoria invece che da zero. Il tool pre-carica il contesto rilevante dal cervello (lavori correlati + learning applicabili) e lo mette in cima al playbook, poi ti guida CONSULT -> DO -> SAVE.
        QUANDO NON USARLO: NOT per eseguire il piano (questo restituisce solo le istruzioni; l'agente ospite le esegue). NOT per cercare contesto puntuale -> usa search / check_learnings direttamente.
        RESTITUISCE: il testo del playbook (markdown) con il bundle di contesto gia' incorporato. Poi save_plan salva IL PIANO che scrivi tu eseguendo il playbook, NON il testo del playbook."""
        try:
            ctx, visible_projects = await _current_workflow_scope()
            return await _build_plan_playbook(
                feature,
                project,
                ctx=ctx,
                visible_projects=visible_projects,
            )
        except ServiceError as e:
            raise_mcp_error(e)

    @mcp.tool()
    async def save_plan(
        project: Annotated[str, Field(min_length=1, max_length=64)],
        title: Annotated[str, Field(min_length=1, max_length=200)],
        body: Annotated[str, Field(min_length=1)],
    ) -> dict[str, Any]:
        """Persist a new plan to the project's docs/plans/ folder and embed it into the brain.

        QUANDO USARLO: hai finito di redigere un piano nuovo (tipicamente al passo SAVE del playbook plan) e vuoi creare il suo carrier. Il tool sceglie il nome file datato/kebab corretto, lo scrive nella cartella giusta, e lo embedda cosi' che la prossima ricerca lo trovi per significato. NON scrivere il file a mano.
        QUANDO NON USARLO: NOT per ottenere il playbook -> usa plan. NOT per artefatti diversi da un piano -> brainstorm in docs/brainstorms/ con save_brainstorm, documento-soluzione in docs/solutions/ con save_compound. NOT per review, approfondimento o modifica di un piano esistente: usa read_file, poi write_file sullo stesso path con if_match_sha256, infine read_file per verificare.
        RESTITUISCE: {path, indexed, embedded} — il percorso del file scritto, se l'indice testuale e' stato aggiornato e se l'embedding e' andato a buon fine (best-effort: il salvataggio riesce anche se l'index/embed fallisce)."""
        try:
            ctx, visible_projects = await _current_workflow_scope()
            return await _save_plan_artifact(
                project,
                title,
                body,
                ctx=ctx,
                visible_projects=visible_projects,
            )
        except ServiceError as e:
            raise_mcp_error(e)

    @mcp.tool()
    async def brainstorm(
        topic: Annotated[str, Field(min_length=1, max_length=2000)],
        project: str | None = None,
    ) -> str:
        """Return a brainstorming playbook (istruzioni) — NON esplora e NON esegue nulla.

        QUANDO USARLO: stai per esplorare un'idea/funzionalita' e vuoi ragionare sul COSA costruire (approcci, tradeoff) prima di pianificare il COME. Il tool pre-carica il contesto rilevante dal cervello (lavori correlati + learning) e lo mette in cima al playbook, poi ti guida CONSULT -> DO (esplora 2-3 approcci concreti, applica YAGNI, una domanda alla volta) -> SAVE.
        QUANDO NON USARLO: NOT per pianificare l'implementazione -> usa plan. NOT per eseguire (questo restituisce solo le istruzioni). NOT per cercare contesto puntuale -> usa search / check_learnings.
        RESTITUISCE: il testo del playbook (markdown) con il bundle di contesto gia' incorporato. Poi save_brainstorm salva IL BRAINSTORM che scrivi tu eseguendo il playbook, NON il testo del playbook."""
        try:
            ctx, visible_projects = await _current_workflow_scope()
            return await _build_brainstorm_playbook(
                topic,
                project,
                ctx=ctx,
                visible_projects=visible_projects,
            )
        except ServiceError as e:
            raise_mcp_error(e)

    @mcp.tool()
    async def save_brainstorm(
        project: Annotated[str, Field(min_length=1, max_length=64)],
        title: Annotated[str, Field(min_length=1, max_length=200)],
        body: Annotated[str, Field(min_length=1)],
    ) -> dict[str, Any]:
        """Persist a brainstorm to the project's docs/brainstorms/ folder and embed it.

        QUANDO USARLO: hai finito di esplorare un'idea (tipicamente al passo SAVE del playbook brainstorm) e vuoi salvarla. Il tool sceglie il nome file datato/kebab corretto, lo scrive nella cartella giusta, e lo embedda cosi' che la prossima ricerca lo trovi per significato. NON scrivere il file a mano.
        QUANDO NON USARLO: NOT per ottenere il playbook -> usa brainstorm. NOT per artefatti diversi da un brainstorm -> piano in docs/plans/ con save_plan, documento-soluzione in docs/solutions/ con save_compound.
        RESTITUISCE: {path, indexed, embedded} — il percorso del file scritto, se l'indice testuale e' stato aggiornato e se l'embedding e' andato a buon fine (best-effort: il salvataggio riesce anche se l'index/embed fallisce)."""
        try:
            ctx, visible_projects = await _current_workflow_scope()
            return await _save_brainstorm_artifact(
                project,
                title,
                body,
                ctx=ctx,
                visible_projects=visible_projects,
            )
        except ServiceError as e:
            raise_mcp_error(e)

    @mcp.tool()
    async def compound(
        what: Annotated[str, Field(min_length=1, max_length=2000)],
        project: str | None = None,
    ) -> str:
        """Return a knowledge-capture playbook (istruzioni) — NON cattura e NON esegue nulla.

        QUANDO USARLO: hai appena risolto un problema / costruito qualcosa e vuoi catturarlo come conoscenza durevole e riusabile. Il tool pre-carica il contesto rilevante dal cervello e ti guida CONSULT -> DO (distilla problema, soluzione, trappole, cosa faresti diversamente) -> SAVE, dove salvi sia il documento-soluzione sia una regola di prevenzione (learning) immediatamente recuperabile.
        QUANDO NON USARLO: NOT prima di aver finito il lavoro (cattura conoscenza gia' verificata, non piani). NOT per pianificare -> usa plan. NOT per eseguire (questo restituisce solo le istruzioni).
        RESTITUISCE: il testo del playbook (markdown) con il bundle di contesto gia' incorporato. Poi save_compound (+ create_learning) salva IL DOCUMENTO-SOLUZIONE che scrivi tu eseguendo il playbook, NON il testo del playbook."""
        try:
            ctx, visible_projects = await _current_workflow_scope()
            return await _build_compound_playbook(
                what,
                project,
                ctx=ctx,
                visible_projects=visible_projects,
            )
        except ServiceError as e:
            raise_mcp_error(e)

    @mcp.tool()
    async def save_compound(
        project: Annotated[str, Field(min_length=1, max_length=64)],
        title: Annotated[str, Field(min_length=1, max_length=200)],
        body: Annotated[str, Field(min_length=1)],
    ) -> dict[str, Any]:
        """Persist a solution doc to the project's docs/solutions/ folder and embed it.

        QUANDO USARLO: hai distillato una soluzione (tipicamente al passo SAVE del playbook compound) e vuoi salvarla come documento-soluzione. Il tool sceglie il nome file datato/kebab corretto, lo scrive in docs/solutions/, e lo embedda cosi' che la prossima ricerca lo trovi per significato. NON scrivere il file a mano. Ricorda di chiamare anche create_learning per la regola di prevenzione riusabile.
        QUANDO NON USARLO: NOT per ottenere il playbook -> usa compound. NOT per artefatti diversi da una soluzione -> piano in docs/plans/ con save_plan, brainstorm in docs/brainstorms/ con save_brainstorm.
        RESTITUISCE: {path, indexed, embedded} — il percorso del file scritto, se l'indice testuale e' stato aggiornato e se l'embedding e' andato a buon fine (best-effort: il salvataggio riesce anche se l'index/embed fallisce)."""
        try:
            ctx, visible_projects = await _current_workflow_scope()
            return await _save_compound_artifact(
                project,
                title,
                body,
                ctx=ctx,
                visible_projects=visible_projects,
            )
        except ServiceError as e:
            raise_mcp_error(e)
