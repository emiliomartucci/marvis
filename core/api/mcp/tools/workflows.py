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
import re
from pathlib import Path
from typing import Annotated, Any

from pydantic import Field

from core.api.mcp._adapter import LOCAL_CTX, acquire_db, raise_mcp_error
from core.api.use_cases._errors import ServiceError

logger = logging.getLogger(__name__)

#: Playbooks ship bundled with the package (versioned with the CLI), one markdown
#: file per workflow. B2 drops more files here without touching the loader. The dir
#: is named ``workflow_playbooks`` (not ``workflows``) to avoid colliding with this
#: ``workflows.py`` module name on the import path.
_PLAYBOOKS_DIR = Path(__file__).parent / "workflow_playbooks"

#: Kebab-case slug for the dated plan filename (mirrors the conventional-commit
#: title rule: strip the ``type:`` prefix, lowercase, collapse to ``a-z0-9-``).
_KEBAB_STRIP_RE = re.compile(r"[^a-z0-9]+")


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


async def _prefetch_brain_context(feature: str, project: str | None) -> str:
    """Pull related prior work + applicable learnings, best-effort, fastapi-free.

    Calls the SAME use_cases the host agent is told to call, so the returned
    playbook already starts grounded instead of merely instructing a lookup. Pure
    best-effort: if search is down (embedding backend 503), the embedder is absent, or anything
    else raises, return a fallback line that tells the agent to run the consult
    itself — the playbook must still be runnable on a brain that is momentarily
    unavailable.

    Returns a markdown block (the ``{brain_context}`` substitution) — never raises.
    """
    try:
        from core.api.use_cases import learnings as learnings_uc
        from core.api.use_cases import search as search_uc

        # search opens its own connections from settings (no request db); learnings
        # needs a read connection.
        search_res = await search_uc.search(LOCAL_CTX, q=feature)
        async with acquire_db() as db:
            learn_res = await learnings_uc.check_learnings(
                LOCAL_CTX, db, query=feature
            )
    except Exception:
        logger.debug("plan prefetch unavailable (non-critical)", exc_info=True)
        return (
            "_Brain prefetch unavailable — run the consult yourself: call "
            "`mcp__marvis__search` + `mcp__marvis__check_learnings` before drafting._"
        )

    return _format_brain_context(search_res, learn_res)


def _format_brain_context(search_res: Any, learn_res: Any) -> str:
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


async def _build_plan_playbook(feature: str, project: str | None = None) -> str:
    """Build the plan playbook text, with the brain prefetch spliced into CONSULT.

    The bundled ``plan.md`` carries the 3-beat skeleton with ``{brain_context}`` and
    ``{feature}`` placeholders; this fills them. The prefetch is best-effort
    (degrades to a self-consult instruction) so the returned playbook is always
    runnable. Module-level + SDK-free so tests call it directly.
    """
    template = _load_playbook("plan")
    brain_context = await _prefetch_brain_context(feature, project)
    return template.replace("{brain_context}", brain_context).replace(
        "{feature}", feature
    )


async def _build_brainstorm_playbook(topic: str, project: str | None = None) -> str:
    """Build the brainstorm playbook text, brain prefetch spliced into CONSULT.

    Twin of :func:`_build_plan_playbook` — same prefetch (best-effort, degrades to a
    self-consult line) so the returned playbook always runs. The bundled
    ``brainstorm.md`` carries ``{brain_context}`` + ``{topic}`` placeholders; this
    fills them. Module-level + SDK-free so tests call it directly.
    """
    template = _load_playbook("brainstorm")
    brain_context = await _prefetch_brain_context(topic, project)
    return template.replace("{brain_context}", brain_context).replace(
        "{topic}", topic
    )


async def _build_compound_playbook(what: str, project: str | None = None) -> str:
    """Build the compound playbook text, brain prefetch spliced into CONSULT.

    Twin of :func:`_build_plan_playbook`. The bundled ``compound.md`` carries
    ``{brain_context}`` + ``{what}`` placeholders; this fills them. The prefetch is
    best-effort (degrades to a self-consult instruction). Module-level + SDK-free so
    tests call it directly.
    """
    template = _load_playbook("compound")
    brain_context = await _prefetch_brain_context(what, project)
    return template.replace("{brain_context}", brain_context).replace("{what}", what)


def _resolve_project_path(project: str) -> Path | None:
    """Resolve a project slug to its metadata dir, fastapi-free.

    Reuses the SAME resolver ``use_cases.search.reindex_project`` uses
    (``routers.projects._find_project_path``), imported function-locally so this
    module never pulls fastapi at import time. Returns ``None`` for an unknown slug.
    """
    from core.api.routers.projects import _find_project_path

    return _find_project_path(project)


async def _save_doc_artifact(
    project: str,
    title: str,
    body: str,
    *,
    subdir: str,
    suffix: str,
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
    project_path = _resolve_project_path(project)
    if project_path is None:
        from core.api.use_cases._errors import NotFoundError

        raise NotFoundError(
            code="project_not_found",
            message=f"Project '{project}' not found",
        )

    docs_dir = project_path / "docs" / subdir
    docs_dir.mkdir(parents=True, exist_ok=True)

    today = _dt.date.today().isoformat()
    slug = _kebab_title(title)
    if suffix and not slug.endswith(suffix):
        stem = f"{slug}{suffix}"
    else:
        stem = slug
    doc_path = docs_dir / f"{today}-{stem}.md"
    doc_path.write_text(body, encoding="utf-8")

    indexed = False
    try:
        from core.api.services import embedding_service

        await embedding_service.index_doc_document_fts(
            file_path=str(doc_path),
            title=title,
            content=body,
            project=project,
            workspace_id=LOCAL_CTX.workspace_id,
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
            workspace_id=LOCAL_CTX.workspace_id,
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
    project: str, title: str, body: str
) -> dict[str, Any]:
    """Persist a plan to ``<project>/docs/plans/<date>-<kebab>-plan.md`` (+ embed).

    Thin adapter over :func:`_save_doc_artifact` — the SAVE-beat persistence callback
    for the ``plan`` workflow. Returns ``{path, embedded}``.
    """
    return await _save_doc_artifact(
        project, title, body, subdir="plans", suffix="-plan"
    )


async def _save_brainstorm_artifact(
    project: str, title: str, body: str
) -> dict[str, Any]:
    """Persist a brainstorm to ``<project>/docs/brainstorms/<date>-<kebab>-brainstorm.md``.

    Thin adapter over :func:`_save_doc_artifact` — the SAVE-beat persistence callback
    for the ``brainstorm`` workflow. Returns ``{path, embedded}``.
    """
    return await _save_doc_artifact(
        project, title, body, subdir="brainstorms", suffix="-brainstorm"
    )


async def _save_compound_artifact(
    project: str, title: str, body: str
) -> dict[str, Any]:
    """Persist a solution doc to ``<project>/docs/solutions/<date>-<kebab>.md`` (+ embed).

    Thin adapter over :func:`_save_doc_artifact` — the SAVE-beat persistence callback
    for the ``compound`` workflow. NOTE: the solutions folder and NO ``-plan``/
    ``-brainstorm`` suffix (just the dated kebab slug). Returns ``{path, embedded}``.
    """
    return await _save_doc_artifact(
        project, title, body, subdir="solutions", suffix=""
    )


def register(mcp) -> None:
    """Register the workflows tool group on the shared FastMCP instance."""

    @mcp.tool()
    async def plan(
        feature: Annotated[str, Field(min_length=1, max_length=2000)],
        project: str | None = None,
    ) -> str:
        """Return a brain-grounded planning playbook for a feature, bug, or improvement.

        QUANDO USARLO: stai per pianificare un lavoro (feature/bug/refactor) e vuoi un piano strutturato che parte dalla memoria invece che da zero. Il tool pre-carica il contesto rilevante dal cervello (lavori correlati + learning applicabili) e lo mette in cima al playbook, poi ti guida CONSULT -> DO -> SAVE.
        QUANDO NON USARLO: NOT per eseguire il piano (questo restituisce solo le istruzioni; l'agente ospite le esegue). NOT per cercare contesto puntuale -> usa search / check_learnings direttamente.
        RESTITUISCE: il testo del playbook (markdown) con il bundle di contesto gia' incorporato; salva il risultato via save_plan."""
        try:
            return await _build_plan_playbook(feature, project)
        except ServiceError as e:
            raise_mcp_error(e)

    @mcp.tool()
    async def save_plan(
        project: Annotated[str, Field(min_length=1, max_length=64)],
        title: Annotated[str, Field(min_length=1, max_length=200)],
        body: Annotated[str, Field(min_length=1)],
    ) -> dict[str, Any]:
        """Persist a plan to the project's docs/plans/ folder and embed it into the brain.

        QUANDO USARLO: hai finito di redigere un piano (tipicamente al passo SAVE del playbook plan) e vuoi salvarlo. Il tool sceglie il nome file datato/kebab corretto, lo scrive nella cartella giusta, e lo embedda cosi' che la prossima ricerca lo trovi per significato. NON scrivere il file a mano.
        QUANDO NON USARLO: NOT per ottenere il playbook -> usa plan. NOT per artefatti diversi da un piano.
        RESTITUISCE: {path, indexed, embedded} — il percorso del file scritto, se l'indice testuale e' stato aggiornato e se l'embedding e' andato a buon fine (best-effort: il salvataggio riesce anche se l'index/embed fallisce)."""
        try:
            return await _save_plan_artifact(project, title, body)
        except ServiceError as e:
            raise_mcp_error(e)

    @mcp.tool()
    async def brainstorm(
        topic: Annotated[str, Field(min_length=1, max_length=2000)],
        project: str | None = None,
    ) -> str:
        """Return a brain-grounded brainstorming playbook to explore WHAT to build.

        QUANDO USARLO: stai per esplorare un'idea/funzionalita' e vuoi ragionare sul COSA costruire (approcci, tradeoff) prima di pianificare il COME. Il tool pre-carica il contesto rilevante dal cervello (lavori correlati + learning) e lo mette in cima al playbook, poi ti guida CONSULT -> DO (esplora 2-3 approcci concreti, applica YAGNI, una domanda alla volta) -> SAVE.
        QUANDO NON USARLO: NOT per pianificare l'implementazione -> usa plan. NOT per eseguire (questo restituisce solo le istruzioni). NOT per cercare contesto puntuale -> usa search / check_learnings.
        RESTITUISCE: il testo del playbook (markdown) con il bundle di contesto gia' incorporato; salva il risultato via save_brainstorm."""
        try:
            return await _build_brainstorm_playbook(topic, project)
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
        QUANDO NON USARLO: NOT per ottenere il playbook -> usa brainstorm. NOT per artefatti diversi da un brainstorm.
        RESTITUISCE: {path, indexed, embedded} — il percorso del file scritto, se l'indice testuale e' stato aggiornato e se l'embedding e' andato a buon fine (best-effort: il salvataggio riesce anche se l'index/embed fallisce)."""
        try:
            return await _save_brainstorm_artifact(project, title, body)
        except ServiceError as e:
            raise_mcp_error(e)

    @mcp.tool()
    async def compound(
        what: Annotated[str, Field(min_length=1, max_length=2000)],
        project: str | None = None,
    ) -> str:
        """Return a brain-grounded playbook to capture a finished solution as durable knowledge.

        QUANDO USARLO: hai appena risolto un problema / costruito qualcosa e vuoi catturarlo come conoscenza durevole e riusabile. Il tool pre-carica il contesto rilevante dal cervello e ti guida CONSULT -> DO (distilla problema, soluzione, trappole, cosa faresti diversamente) -> SAVE, dove salvi sia il documento-soluzione sia una regola di prevenzione (learning) immediatamente recuperabile.
        QUANDO NON USARLO: NOT prima di aver finito il lavoro (cattura conoscenza gia' verificata, non piani). NOT per pianificare -> usa plan. NOT per eseguire (questo restituisce solo le istruzioni).
        RESTITUISCE: il testo del playbook (markdown) con il bundle di contesto gia' incorporato; salva il risultato via save_compound + create_learning."""
        try:
            return await _build_compound_playbook(what, project)
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
        QUANDO NON USARLO: NOT per ottenere il playbook -> usa compound. NOT per artefatti diversi da una soluzione.
        RESTITUISCE: {path, indexed, embedded} — il percorso del file scritto, se l'indice testuale e' stato aggiornato e se l'embedding e' andato a buon fine (best-effort: il salvataggio riesce anche se l'index/embed fallisce)."""
        try:
            return await _save_compound_artifact(project, title, body)
        except ServiceError as e:
            raise_mcp_error(e)
