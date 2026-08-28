# v1.0.0 - 2026-05-27 - S1 F1.2: search use_cases extracted from router
"""Search use_cases — pure domain logic, transport-agnostic (no ``fastapi``).

Sibling of :mod:`core.api.use_cases.learnings` (the S1 "collapse runtime"
TEMPLATE). One pure async function per operation; the HTTP router becomes a thin
adapter that resolves identity into a :class:`CallerContext`, calls these
functions, and maps :class:`ServiceError` -> ``HTTPException`` via
``routers/_adapter.to_http``. The Python MCP surface (later) calls the SAME
functions with ``CallerContext.local_single_user()``. One implementation, no fork.

How the three TEMPLATE decisions land on the search domain:

DECISION 1 — Visibility. Search is workspace-scoped first, then tenant-grant
    filtered through ``services.access_grants`` when multi-user visibility is
    active. This keeps the MCP and HTTP paths on the same predicate instead of
    returning hidden projects through the semantic/KG retrievers.

DECISION 2 — ``deep`` KG enrichment. N/A here. Search exposes no ``deep`` param;
    the hybrid path already returns the KG-aware ``edge_path`` fields inline.

DECISION 3 — Errors. The ``503`` raised by the *router itself* when the embedding
    backend is unavailable ("Semantic search is temporarily unavailable") becomes a domain
    :class:`ServiceUnavailableError` (``http_status = 503``). The ``RuntimeError``
    surfaced by ``embedding_service.search_by_type`` is likewise wrapped into a
    ``ServiceUnavailableError`` so the adapter need only translate ServiceError.

Signature note (faithful deviation from the learnings template): these functions
take ``(ctx, *typed_args)`` and DO NOT receive a request-scoped ``db``. Search and
reindex never use the request connection pool — they open their own connections
from ``settings.db_path`` / ``settings.vec0_path`` (``search_by_type`` documents
why: per-connection serialization in aiosqlite). Keeping that property unchanged
is the whole point of the reindex-route guard test (no read-only pool dependency).

Service imports (embedding client, KG hybrid_search, ``db._configure_connection``)
are kept FUNCTION-LOCAL exactly as the original router did. ``hybrid_search`` and
``embedding_service`` are services (fastapi-free) and could import at module top,
but ``PROJECT_DIRS`` / ``_read_project_yaml`` live in ``routers.projects`` whose
module imports ``fastapi``; importing them lazily keeps THIS module fastapi-free
at import time (the property the import-linter contract + the smoke test assert).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import uuid
from collections.abc import Sequence
from pathlib import Path

import aiosqlite

from core.api.config import settings
from core.api.models.search import SearchHit, SearchResponse
from core.api.services import access_grants
from core.api.services.repository_authority import historical_repository_notice
from core.api.use_cases._context import (
    CallerContext,
    require_role_ctx,
    require_workspace_ctx,
)
from core.api.use_cases._errors import NotFoundError, ServiceUnavailableError

logger = logging.getLogger(__name__)

# Stay below the sidecar's 250k-character document ceiling even for ASCII.
FILE_LARGE_BYTES = 200_000
FILE_CHUNKING_MAX_BYTES = 2_000_000
FILE_LARGE_EMBED_CHARS = 1_500
FILE_REINDEX_EMBED_BATCH_SIZE = 2
SEMANTIC_FILE_STATE_POPULATOR = "semantic"


async def _record_semantic_file_state(
    db: aiosqlite.Connection, *, path: str, sha256: str
) -> None:
    """Persist proof that the semantic vector matches the current file bytes."""
    try:
        await db.execute(
            "INSERT INTO file_state(path, populator, sha256, indexed_at) "
            "VALUES (?, ?, ?, datetime('now')) "
            "ON CONFLICT(path, populator) DO UPDATE SET "
            "sha256=excluded.sha256, indexed_at=excluded.indexed_at",
            [path, SEMANTIC_FILE_STATE_POPULATOR, sha256],
        )
    except aiosqlite.OperationalError as exc:
        if "no such table" not in str(exc).lower():
            raise


async def _forget_semantic_file_state(
    db: aiosqlite.Connection, *, path: str
) -> None:
    """Remove semantic freshness proof when a file leaves the search index."""
    try:
        await db.execute(
            "DELETE FROM file_state WHERE path=? AND populator=?",
            [path, SEMANTIC_FILE_STATE_POPULATOR],
        )
    except aiosqlite.OperationalError as exc:
        if "no such table" not in str(exc).lower():
            raise


def _chunking_max_file_bytes() -> int:
    raw = os.environ.get("MARVIS_CHUNKING_MAX_FILE_BYTES")
    if not raw:
        return FILE_CHUNKING_MAX_BYTES
    try:
        value = int(raw)
    except ValueError:
        logger.warning(
            "Invalid MARVIS_CHUNKING_MAX_FILE_BYTES=%r; using default %s",
            raw,
            FILE_CHUNKING_MAX_BYTES,
        )
        return FILE_CHUNKING_MAX_BYTES
    return max(FILE_LARGE_BYTES, value)

VALID_REINDEX_TYPES = {
    "tasks",
    "projects",
    "files",
    "handoffs",
    "learnings",
    "inbox_items",
    "audits",
    "all",
}

# Background task set (prevents GC of fire-and-forget reindex-all task).
_bg_tasks: set[asyncio.Task] = set()

# U5: at most one lazy first-search self-heal build in flight at a time.
_lazy_build_inflight = False


async def _documents_empty(workspace_id: str) -> bool:
    """Cheap unbuilt-index probe: the ``documents`` corpus has no rows yet.

    ``documents`` is a plain table (no vec0 extension needed), so this stays
    cheap. Any error (table missing, locked) → treat as "cannot tell" and do
    NOT build (avoid a build storm on an unrelated failure).
    """
    try:
        db = await aiosqlite.connect(settings.db_path)
    except Exception:  # noqa: BLE001
        return False
    try:
        cur = await db.execute(
            "SELECT 1 FROM documents WHERE workspace_id = ? LIMIT 1", [workspace_id]
        )
        return (await cur.fetchone()) is None
    except Exception:  # noqa: BLE001
        return False
    finally:
        await db.close()


async def _maybe_kick_lazy_build(workspace_id: str) -> bool:
    """U5/U6: on a zero-result search over an UNBUILT index, start a self-healing
    background build. Returns True iff a build was actually started.

    Fully fail-safe (never raises into ``search``) and cheap-gated: the embedding
    backend must be available, free RAM above the U6 floor, no build already in
    flight, and the ``documents`` corpus genuinely empty (so a built-but-no-match
    search never triggers a needless rebuild).
    """
    global _lazy_build_inflight
    try:
        if _lazy_build_inflight:
            return False
        from core.api.services import embedding_service

        if not embedding_service.is_available():
            return False
        ram_ok, reason = embedding_service.background_index_ram_ok()
        if not ram_ok:
            logger.info("lazy index build deferred: %s", reason)
            return False
        if not await _documents_empty(workspace_id):
            return False

        _lazy_build_inflight = True
        task = asyncio.create_task(_reindex_all_bg(settings.db_path))
        _bg_tasks.add(task)

        def _done(t: asyncio.Task) -> None:
            global _lazy_build_inflight
            _lazy_build_inflight = False
            _bg_tasks.discard(t)

        task.add_done_callback(_done)
        logger.info("lazy index build started (workspace=%s)", workspace_id)
        return True
    except Exception:  # noqa: BLE001 — self-healing must never break search
        _lazy_build_inflight = False
        return False


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def _build_response(
    grouped: dict[str, list[dict]],
    q: str,
    meta: dict[str, object] | None,
) -> SearchResponse:
    def to_hits(doc_type: str, items: list[dict]) -> list[SearchHit]:
        return [
            SearchHit(
                doc_type=doc_type,  # type: ignore[arg-type]
                doc_id=item["doc_id"],
                title=item["title"],
                project=item["project"],
                score=item.get("score", 0.5),
                salience=item.get("salience", 0.5),
                path=item.get("path"),
                status=item.get("status"),
                # Phase 6.5 A extensions (None for legacy semantic-only path).
                edge_path=item.get("edge_path"),
                edge_path_summary=item.get("edge_path_summary"),
                rrf_score=item.get("rrf_score"),
                authority_notice=historical_repository_notice(item.get("span_text")),
                # A-span (MARVIS_SEARCH_SPANS): the engine attaches these in
                # search_by_type/_attach_spans; dropping them here made every
                # MCP/HTTP consumer see null spans (b6 bug, caught by the
                # v2a.1 acceptance run). A parity test pins model ↔ to_hits.
                span_text=item.get("span_text"),
                span_path=item.get("span_path"),
                span_line_start=item.get("span_line_start"),
                span_line_end=item.get("span_line_end"),
            )
            for item in items
        ]

    tasks = to_hits("task", grouped.get("task", []))
    projects = to_hits("project", grouped.get("project", []))
    files = to_hits("file", grouped.get("file", []))
    handoffs = to_hits("handoff", grouped.get("handoff", []))
    learnings = to_hits("learning", grouped.get("learning", []))
    inbox_items = to_hits("inbox_item", grouped.get("inbox_item", []))
    audits = to_hits("audit", grouped.get("audit", []))

    suggested = None
    semantic_available = None
    semantic_reason = None
    if meta is not None:
        maybe = meta.get("suggested_next_tool")
        if isinstance(maybe, list) and maybe:
            suggested = [str(x) for x in maybe]
        # F1: surface semantic availability ALWAYS, independent of total hits.
        sa = meta.get("semantic_available")
        if isinstance(sa, bool):
            semantic_available = sa
        sr = meta.get("semantic_reason")
        if isinstance(sr, str):
            semantic_reason = sr

    total = (
        len(tasks)
        + len(projects)
        + len(files)
        + len(handoffs)
        + len(learnings)
        + len(inbox_items)
        + len(audits)
    )

    # Fase 2 mielinizzazione U3 (R6/KTD2): ONE structured nudge entry on the
    # existing affordance channel, shadow/on + non-empty only — mode off keeps
    # the response byte-identical (AE5). Never prose, never per-hit.
    if total > 0:
        from core.api.use_cases.feedback import MEMORY_FEEDBACK_NUDGE, nudge_enabled

        if nudge_enabled():
            suggested = [*(suggested or []), MEMORY_FEEDBACK_NUDGE]

    return SearchResponse(
        tasks=tasks,
        projects=projects,
        files=files,
        handoffs=handoffs,
        learnings=learnings,
        inbox_items=inbox_items,
        audits=audits,
        total=total,
        query=q,
        suggested_next_tool=suggested,
        semantic_available=semantic_available,
        semantic_reason=semantic_reason,
    )


# ---------------------------------------------------------------------------
# Use cases
# ---------------------------------------------------------------------------


async def search(
    ctx: CallerContext,
    *,
    q: str,
    hybrid: bool = True,
    limit: int = 20,
    as_of: str | None = None,
) -> SearchResponse:
    """Hybrid (default) or semantic search across the KG and embedding index.

    Phase 6.5 A: when ``hybrid=True`` the semantic retriever and the FTS5
    KG retriever run in parallel and rankings are fused with weighted RRF; if
    either retriever fails the other still returns results (graceful degradation).
    When ``hybrid=False`` the endpoint returns the original semantic-only output
    shape (no ``edge_path`` / ``rrf_score``).

    Temporal (Track 2 #1-S2): ``as_of`` only affects the LEARNING lane and only
    when ``MARVIS_TEMPORAL_MEMORY`` is ON — superseded learnings are excluded
    (default) or reconstructed as-of a date. Flag OFF or ``as_of=None`` → unchanged.
    The semantic-only branch (``hybrid=False``) does NOT apply the temporal filter
    (legacy back-compat path); the default hybrid path does.

    Raises :class:`ServiceUnavailableError` (503) when the embedding backend is
    unavailable on the semantic-only path, or when ``search_by_type`` reports a
    backend ``RuntimeError`` (DECISION 3).
    """
    workspace_id = require_workspace_ctx(ctx)

    if hybrid:
        from core.api.services.kg.hybrid_search import hybrid_search as _hybrid_search

        grouped, meta = await _hybrid_search(
            q=q,
            workspace_id=workspace_id,
            db_path=settings.db_path,
            vec0_path=settings.vec0_path,
            limit=limit,
            graph_lane=settings.graph_lane_enabled,
            graph_lane_weight=settings.graph_lane_weight,
            graph_lane_seeds=settings.graph_lane_seeds,
            graph_lane_fanout=settings.graph_lane_fanout,
            as_of=as_of,
        )
        # gh#37: a zero-result hybrid search over an EMPTY corpus is a ready
        # (empty) index, NOT "index-building". The old code kicked a build
        # whenever `documents` was empty and flipped semantic_available →
        # false/index-building — but a build over 0 docs adds no rows, so
        # `documents` stayed empty and the state never cleared: a fresh hosted
        # tenant was stuck `index-building` forever. The embedding backend is up
        # (is_available) and there is simply nothing indexed → report the empty
        # index as ready so the first query returns semantic_available:true. The
        # self-healing background build still runs to populate a tenant that has
        # pending source (a no-op on a truly empty one).
        if not any(grouped.values()):
            from core.api.services import embedding_service

            if await _documents_empty(workspace_id) and embedding_service.is_available():
                await _maybe_kick_lazy_build(workspace_id)
                meta = {
                    **(meta or {}),
                    "semantic_available": True,
                    "semantic_reason": None,
                }
        grouped = await access_grants.filter_search_grouped(ctx, grouped)
        return _build_response(grouped, q, meta=meta)

    # Legacy semantic-only branch (backward compat).
    from core.api.services import embedding_service

    if not embedding_service.is_available():
        raise ServiceUnavailableError(
            code="embedding_unavailable",
            message="Semantic search is temporarily unavailable",
        )

    try:
        grouped = await embedding_service.search_by_type(
            query=q,
            workspace_id=workspace_id,
            db_path=settings.db_path,
            vec0_path=settings.vec0_path,
            top_k=5,
        )
    except RuntimeError as e:
        raise ServiceUnavailableError(code="embedding_unavailable", message=str(e))

    grouped = await access_grants.filter_search_grouped(ctx, grouped)
    return _build_response(grouped, q, meta=None)


def _reindex_queue_dir() -> Path:
    """Return the tenant-local durable queue directory for reindex jobs."""
    configured = os.environ.get("MARVIS_REINDEX_QUEUE_DIR")
    if configured:
        return Path(configured).expanduser()
    return Path(settings.db_path).expanduser().parent / "reindex-queue"


def _fsync_directory(directory: Path) -> None:
    directory_fd = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _write_reindex_queue_job(
    job: dict[str, object],
    *,
    queue_dir: Path | None = None,
) -> Path:
    """Crash-durably publish one job with an atomic rename."""
    resolved_queue_dir = queue_dir or _reindex_queue_dir()
    resolved_queue_dir.mkdir(parents=True, exist_ok=True)
    _fsync_directory(resolved_queue_dir.parent)
    job_id = str(job["id"])
    target = resolved_queue_dir / f"{job_id}.json"
    temporary = resolved_queue_dir / f".{job_id}.{uuid.uuid4().hex}.tmp"
    try:
        payload = json.dumps(job, sort_keys=True, separators=(",", ":")) + "\n"
        with temporary.open("w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        _fsync_directory(resolved_queue_dir)
    finally:
        temporary.unlink(missing_ok=True)
    return target


async def queue_reindex(
    ctx: CallerContext,
    *,
    type: str = "all",
) -> dict:
    """Persist an operator reindex request for the dedicated worker."""
    require_role_ctx(ctx, "operator", "admin", "super_admin")
    workspace_id = require_workspace_ctx(ctx)
    job_id = str(uuid.uuid4())
    queue_path = _write_reindex_queue_job(
        {
            "id": job_id,
            "kind": "type",
            "type": type,
            "workspace_id": workspace_id,
        }
    )
    return {
        "status": "queued",
        "type": type,
        "job_id": job_id,
        "queue_path": str(queue_path),
    }


async def trigger_reindex(
    ctx: CallerContext,
    *,
    type: str = "all",
) -> dict:
    """Compatibility entry point for the persistent reindex queue."""
    return await queue_reindex(ctx, type=type)


def _authorized_reindex_file_paths(
    paths: Sequence[str | Path],
    visible_projects: set[str] | None,
) -> list[str]:
    """Canonicalize remote paths and require one visible project mapping each.

    ``None`` is the trusted local stdio contract and preserves the legacy path
    forms. Remote callers receive a concrete grant set: an absent, ambiguous, or
    non-visible mapping fails closed before a queue job can read the filesystem.
    """
    if visible_projects is None:
        return [str(path) for path in paths]

    from core.api.routers.projects import PROJECT_DIRS

    try:
        project_dirs = [Path(base).expanduser().resolve() for base in PROJECT_DIRS]
    except (OSError, RuntimeError) as exc:
        raise NotFoundError(
            code="reindex_path_not_authorized",
            message="Reindex path is not mapped unambiguously to a visible project",
        ) from exc
    authorized: list[str] = []
    seen: set[str] = set()
    for raw_path in paths:
        path = Path(raw_path).expanduser()
        candidates: list[Path] = []
        try:
            if path.is_absolute():
                candidates.append(path.resolve())
            elif len(path.parts) >= 2:
                project_slug = path.parts[0]
                for base in project_dirs:
                    project_root = base / project_slug
                    if project_root.is_dir() and not project_root.is_symlink():
                        candidates.append((base / path).resolve())
        except (OSError, RuntimeError):
            candidates = []

        mappings: dict[tuple[str, str], tuple[str, str, str, str]] = {}
        for candidate in candidates:
            for base in project_dirs:
                metadata = _file_reindex_metadata_from_absolute_path(candidate, [base])
                if metadata is not None:
                    mappings[(metadata[0], metadata[1])] = metadata

        if len(mappings) != 1:
            raise NotFoundError(
                code="reindex_path_not_authorized",
                message="Reindex path is not mapped unambiguously to a visible project",
            )
        (project, canonical_path), = mappings
        if project not in visible_projects:
            raise NotFoundError(
                code="reindex_path_not_authorized",
                message="Reindex path is not mapped unambiguously to a visible project",
            )
        if canonical_path not in seen:
            seen.add(canonical_path)
            authorized.append(canonical_path)
    return authorized


async def queue_reindex_file_paths(
    ctx: CallerContext,
    *,
    paths: Sequence[str | Path],
    visible_projects: set[str] | None = None,
) -> dict:
    """Persist explicit file paths for the dedicated reindex worker."""
    require_role_ctx(ctx, "operator", "admin", "super_admin")
    workspace_id = require_workspace_ctx(ctx)
    authorized_paths = _authorized_reindex_file_paths(paths, visible_projects)
    job_id = str(uuid.uuid4())
    queue_path = _write_reindex_queue_job(
        {
            "id": job_id,
            "kind": "paths",
            "paths": authorized_paths,
            "workspace_id": workspace_id,
        }
    )
    return {
        "status": "queued",
        "type": "files",
        "job_id": job_id,
        "queue_path": str(queue_path),
    }


async def reindex_file_paths(
    ctx: CallerContext,
    *,
    paths: Sequence[str | Path],
    visible_projects: set[str] | None = None,
) -> dict:
    """Compatibility entry point for persistent delta reindex requests."""
    return await queue_reindex_file_paths(
        ctx,
        paths=paths,
        visible_projects=visible_projects,
    )


async def _open_reindex_db() -> aiosqlite.Connection:
    """Open the single writer connection used by manual reindex operations."""
    from core.api.db import _configure_connection, resolve_vec0_loadable

    db = await aiosqlite.connect(settings.db_path)
    await _configure_connection(db)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA busy_timeout=60000")

    load_arg, vec_found = resolve_vec0_loadable()
    if vec_found and load_arg is not None:
        await db._execute(db._conn.enable_load_extension, True)
        await db.execute("SELECT load_extension(?)", [load_arg])
        await db.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS vec_documents USING vec0(
                doc_id INTEGER PRIMARY KEY,
                embedding float[512]
            )
        """)

    return db


async def _reindex_type(doc_type: str, workspace_id: str) -> dict:
    """Reindex a single document type. Returns stats dict."""
    db = await _open_reindex_db()

    try:
        if doc_type == "handoffs":
            return await _reindex_handoffs(workspace_id, db, db)
        elif doc_type == "tasks":
            return await _reindex_tasks(workspace_id, db, db)
        elif doc_type == "projects":
            return await _reindex_projects(workspace_id, db, db)
        elif doc_type == "files":
            return await _reindex_files(workspace_id, db, db)
        elif doc_type == "learnings":
            return await _reindex_learnings(workspace_id, db, db)
        elif doc_type == "inbox_items":
            return await _reindex_inbox_items(workspace_id, db, db)
        elif doc_type == "audits":
            return await _reindex_audits(workspace_id, db, db)
        return {"indexed": 0}
    finally:
        await db.close()


async def _reindex_type_bg(doc_type: str, workspace_id: str) -> None:
    logger.info("Reindex %s: starting background job", doc_type)
    try:
        result = await _reindex_type(doc_type, workspace_id)
        logger.info("Reindex %s: %s", doc_type, result)
    except Exception:
        logger.exception("Reindex %s failed", doc_type)


async def _reindex_all_bg(db_path: str) -> None:
    """Background task: reindex all doc types.

    Uses a SINGLE connection with vec extension loaded as both db and vec_db.
    Two separate writers to the same DB cause 'database is locked' errors.
    """
    from core.api.db import _configure_connection, resolve_vec0_loadable

    logger.info("Reindex all: starting background job")
    try:
        db = await aiosqlite.connect(db_path)
        await _configure_connection(db)
        db.row_factory = aiosqlite.Row
        # Higher busy_timeout for background reindex — avoids blocking regular requests
        await db.execute("PRAGMA busy_timeout=60000")
        # Load vec extension on the same connection (cross-platform: .so/.dylib)
        load_arg, vec_found = resolve_vec0_loadable()
        if vec_found and load_arg is not None:
            await db._execute(db._conn.enable_load_extension, True)
            await db.execute("SELECT load_extension(?)", [load_arg])
            await db.execute("""
                CREATE VIRTUAL TABLE IF NOT EXISTS vec_documents USING vec0(
                    doc_id INTEGER PRIMARY KEY,
                    embedding float[512]
                )
            """)

        workspace_id = "ws_default"
        # Pass same connection as both db and vec_db — single writer, no locking
        for doc_type, fn in [
            ("handoffs", _reindex_handoffs),
            ("tasks", _reindex_tasks),
            ("projects", _reindex_projects),
            ("files", _reindex_files),
            ("learnings", _reindex_learnings),
            ("inbox_items", _reindex_inbox_items),
            ("audits", _reindex_audits),
        ]:
            try:
                result = await fn(workspace_id, db, db)
                logger.info("Reindex %s: %s", doc_type, result)
            except Exception:
                logger.exception("Reindex %s failed", doc_type)

        await db.close()
        logger.info("Reindex all: background job complete")
    except Exception:
        logger.exception("Reindex all: background job failed")


async def _unchanged_and_embedded(
    db: aiosqlite.Connection,
    vec_db: aiosqlite.Connection,
    file_path: str,
    h: str,
) -> bool:
    """True only when the stored hash matches AND the vector row exists.

    Self-heal (2026-08-05, PR #197 generalized): every reindex loop skipped on
    hash equality alone, so a document whose vector was lost stayed unembedded
    forever — 31 real files plus one learning sat that way for weeks. Unchanged
    now means: same content AND its embedding is actually there. A missing or
    unqueryable vec table means the index needs building, never skipping.
    """
    cur = await db.execute(
        "SELECT id, content_hash FROM documents WHERE file_path = ?", [file_path]
    )
    existing = await cur.fetchone()
    if not existing or existing["content_hash"] != h:
        return False
    try:
        vec_cur = await vec_db.execute(
            "SELECT 1 FROM vec_documents WHERE doc_id = ?", [existing["id"]]
        )
        return (await vec_cur.fetchone()) is not None
    except Exception:  # noqa: BLE001 - no vec table -> rebuild, never skip
        return False


async def _reindex_tasks(
    workspace_id: str, db: aiosqlite.Connection, vec_db: aiosqlite.Connection
) -> dict:
    """Batch-embed tasks to minimize remote embedding API calls (free tier: 3 RPM).

    Pattern: read → commit (release locks) → API call → write → commit.
    Never hold DB locks during external API calls.
    """
    from core.api.services.embedding_service import (
        content_hash,
        embed_texts,
        refresh_documents_fts_row,
        serialize_f32,
    )

    # --- Read phase: gather tasks + filter unchanged ---
    cur = await db.execute(
        "SELECT id, title, project, status FROM tasks WHERE deleted_at IS NULL AND workspace_id = ?",
        [workspace_id],
    )
    rows = await cur.fetchall()

    to_embed: list[tuple] = []  # (row_dict, content, hash, file_path)
    for row in rows:
        content = f"{row['title']}\nStatus: {row['status']}\nProject: {row['project']}"
        h = content_hash(content)
        file_path = f"task:{row['id']}"
        if await _unchanged_and_embedded(db, vec_db, file_path, h):
            continue
        # Materialize row data (aiosqlite.Row refs may not survive commit)
        to_embed.append((dict(row), content, h, file_path))

    # Release any implicit transaction from reads BEFORE the embedding API call
    await db.commit()

    if not to_embed:
        return {"indexed": 0, "skipped": len(rows), "total": len(rows)}

    # --- Embed + write in small batches ---
    indexed = 0
    batch_size = 64
    for i in range(0, len(to_embed), batch_size):
        batch = to_embed[i : i + batch_size]
        texts = [content for _, content, _, _ in batch]

        # API phase: no DB lock held
        embeddings = await embed_texts(texts, input_type="document")

        # Write phase: quick burst of writes, then commit
        for (row_d, content, h, file_path), embedding in zip(batch, embeddings):
            await db.execute(
                """INSERT INTO documents (file_path, project, workspace_id, doc_type, doc_title, content_hash)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(file_path) DO UPDATE SET
                     content_hash = excluded.content_hash,
                     project = excluded.project,
                     workspace_id = excluded.workspace_id,
                     doc_type = excluded.doc_type,
                     doc_title = excluded.doc_title""",
                [
                    file_path,
                    row_d["project"] or "",
                    workspace_id,
                    "task",
                    row_d["title"],
                    h,
                ],
            )
            cur3 = await db.execute(
                "SELECT id FROM documents WHERE file_path = ?", [file_path]
            )
            doc_row = await cur3.fetchone()
            doc_id = doc_row["id"]
            # B-fix (MARVIS_FTS_BODIES): repair the trigger-degraded FTS row.
            await refresh_documents_fts_row(
                db, doc_id=doc_id, title=row_d["title"], content=content
            )

            await vec_db.execute("DELETE FROM vec_documents WHERE doc_id = ?", [doc_id])
            await vec_db.execute(
                "INSERT INTO vec_documents (doc_id, embedding) VALUES (?, ?)",
                [doc_id, serialize_f32(embedding)],
            )
            indexed += 1

        await db.commit()
        if db is not vec_db:
            await vec_db.commit()

        # Rate limit: 20s between batches respects the remote backend 3 RPM free tier
        if i + batch_size < len(to_embed):
            await asyncio.sleep(20)

    return {"indexed": indexed, "total": len(rows)}


async def _reindex_projects(
    workspace_id: str, db: aiosqlite.Connection, vec_db: aiosqlite.Connection
) -> dict:
    """Batch-embed projects from filesystem (project.yaml). No DB projects table."""
    from core.api.routers.projects import PROJECT_DIRS, _read_project_yaml
    from core.api.services.embedding_service import (
        content_hash,
        embed_texts,
        refresh_documents_fts_row,
        serialize_f32,
    )

    # Collect all projects
    items: list[tuple[str, str, str]] = []  # (slug, name, content)
    for base in PROJECT_DIRS:
        if not base.exists():
            continue
        for project_dir in sorted(base.iterdir()):
            if not project_dir.is_dir() or project_dir.is_symlink():
                continue
            yaml_data = _read_project_yaml(project_dir)
            if not yaml_data:
                continue
            slug = yaml_data.get("project") or project_dir.name
            name = yaml_data.get("description") or slug
            description = yaml_data.get("description") or ""
            content = f"{slug}\n{name}\n{description}"
            items.append((slug, name, content))

    # Filter unchanged
    to_embed: list[tuple[str, str, str, str]] = []  # (slug, name, content, hash)
    for slug, name, content in items:
        h = content_hash(content)
        file_path = f"project:{slug}"
        if await _unchanged_and_embedded(db, vec_db, file_path, h):
            continue
        to_embed.append((slug, name, content, h))

    # Release implicit transaction from reads before API call
    await db.commit()

    if not to_embed:
        return {"indexed": 0, "skipped": len(items), "total": len(items)}

    # Batch embed all at once (projects are few, usually <50)
    texts = [content for _, _, content, _ in to_embed]
    embeddings = await embed_texts(texts, input_type="document")

    indexed = 0
    for (slug, name, content, h), embedding in zip(to_embed, embeddings):
        file_path = f"project:{slug}"
        await db.execute(
            """INSERT INTO documents (file_path, project, workspace_id, doc_type, doc_title, content_hash)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(file_path) DO UPDATE SET
                 content_hash = excluded.content_hash,
                 project = excluded.project,
                 workspace_id = excluded.workspace_id,
                 doc_type = excluded.doc_type,
                 doc_title = excluded.doc_title""",
            [file_path, slug, workspace_id, "project", name, h],
        )
        cur2 = await db.execute(
            "SELECT id FROM documents WHERE file_path = ?", [file_path]
        )
        doc_row = await cur2.fetchone()
        doc_id = doc_row["id"]
        # B-fix (MARVIS_FTS_BODIES): repair the trigger-degraded FTS row.
        await refresh_documents_fts_row(db, doc_id=doc_id, title=name, content=content)

        await vec_db.execute("DELETE FROM vec_documents WHERE doc_id = ?", [doc_id])
        await vec_db.execute(
            "INSERT INTO vec_documents (doc_id, embedding) VALUES (?, ?)",
            [doc_id, serialize_f32(embedding)],
        )
        indexed += 1

    await db.commit()
    if db is not vec_db:
        await vec_db.commit()

    return {"indexed": indexed, "total": len(items)}


def _file_reindex_item_from_path(
    path: Path,
    project_dirs: Sequence[Path],
) -> tuple[str, str, str, str, str, str] | None:
    """Return the file-reindex item for one markdown path, or None if skipped."""
    item, _ = _file_reindex_item_or_skip_from_path(path, project_dirs)
    return item


def _file_reindex_item_or_skip_from_path(
    path: Path,
    project_dirs: Sequence[Path],
) -> tuple[tuple[str, str, str, str, str, str] | None, dict[str, str] | None]:
    """Return a file-reindex item and an observable structural skip reason."""
    meta = _file_reindex_metadata_from_path(path, project_dirs)
    if meta is None:
        return None, None
    project, fpath, title, doc_type = meta
    read_path = Path(fpath)
    if read_path.is_symlink():
        return None, {"file": fpath, "reason": "symlink"}

    try:
        size = read_path.stat().st_size
        if size > FILE_LARGE_BYTES:
            from core.api.services.embedding_service import chunking_enabled

            if not chunking_enabled():
                return None, {"file": fpath, "reason": "skipped_too_large"}
            chunking_max_bytes = _chunking_max_file_bytes()
            if size > chunking_max_bytes:
                return None, {
                    "file": fpath,
                    "reason": "skipped_too_large_chunking",
                    "size_bytes": str(size),
                    "max_bytes": str(chunking_max_bytes),
                }
        raw = read_path.read_bytes()
        text = raw.decode("utf-8", errors="replace")
    except OSError:
        return None, {"file": fpath, "reason": "read_error"}

    source_sha256 = hashlib.sha256(raw).hexdigest()
    return (project, fpath, title, text, doc_type, source_sha256), None


def _file_reindex_metadata_from_path(
    path: Path,
    project_dirs: Sequence[Path],
) -> tuple[str, str, str, str] | None:
    """Return stable file-lane metadata for an existing or deleted markdown path."""
    for candidate in _file_reindex_path_candidates(path, project_dirs):
        meta = _file_reindex_metadata_from_absolute_path(candidate, project_dirs)
        if meta is not None:
            return meta
    return None


def _file_reindex_path_candidates(
    path: Path,
    project_dirs: Sequence[Path],
) -> list[Path]:
    """Resolve absolute, projects-root relative, and project-relative file paths."""
    if path.is_absolute():
        return [path]

    candidates: list[Path] = []
    for base in project_dirs:
        candidates.append(base / path)
        try:
            project_dirs_under_base = sorted(
                p for p in base.iterdir() if p.is_dir() and not p.is_symlink()
            )
        except OSError:
            project_dirs_under_base = []
        candidates.extend(project_dir / path for project_dir in project_dirs_under_base)

    seen: set[str] = set()
    out: list[Path] = []
    for candidate in candidates:
        key = str(candidate)
        if key in seen:
            continue
        seen.add(key)
        out.append(candidate)
    return out


def _file_reindex_metadata_from_absolute_path(
    path: Path,
    project_dirs: Sequence[Path],
) -> tuple[str, str, str, str] | None:
    """Return file-lane metadata for a path already resolved under projects_root."""
    if path.suffix.lower() != ".md":
        return None

    project_dir: Path | None = None
    rel: Path | None = None
    for base in project_dirs:
        try:
            base_rel = path.relative_to(base)
        except ValueError:
            continue
        if len(base_rel.parts) < 2:
            return None
        project_dir = base / base_rel.parts[0]
        rel = Path(*base_rel.parts[1:])
        break

    if project_dir is None or rel is None:
        return None
    if any(part.startswith(".") for part in rel.parts[:-1]):
        return None

    doc_type = "file"
    if rel.parts[:1] == ("memory",) and path.name.startswith("handoff-"):
        doc_type = "handoff"

    title = rel.with_suffix("").as_posix().replace("/", " / ").replace("-", " ").strip()
    return project_dir.name, str(path), title, doc_type


async def _refresh_delta_documents_fts_row(
    db: aiosqlite.Connection,
    *,
    doc_id: int | str,
    title: str,
    content: str,
) -> None:
    """Write the real file body to FTS for delta-indexed files.

    The hosted watcher is the freshness path. If this stays gated by
    MARVIS_FTS_BODIES, exact post-edit canaries depend on whole-doc/chunk vector
    ranking and can miss fresh edits. Keep the broader global flag unchanged, but
    make the delta file lane immediately literal-searchable.
    """
    try:
        await db.execute("DELETE FROM documents_fts WHERE rowid = ?", [doc_id])
        await db.execute(
            "INSERT INTO documents_fts(rowid, doc_id, title, content) VALUES (?, ?, ?, ?)",
            [doc_id, doc_id, title or "", content[:500_000]],
        )
    except aiosqlite.OperationalError as exc:
        logger.warning("delta documents_fts refresh degraded (doc_id=%s): %s", doc_id, exc)


async def _delete_file_paths(
    workspace_id: str,
    db: aiosqlite.Connection,
    vec_db: aiosqlite.Connection,
    paths: Sequence[str | Path],
) -> dict:
    """Remove deleted markdown paths from the file search index."""
    from core.api.routers.projects import PROJECT_DIRS

    seen: set[str] = set()
    candidates: list[str] = []
    for raw_path in sorted(paths, key=lambda p: str(p)):
        path = Path(raw_path).expanduser()
        meta = _file_reindex_metadata_from_path(path, PROJECT_DIRS)
        if meta is None:
            continue
        _, fpath, _, _ = meta
        if fpath in seen:
            continue
        seen.add(fpath)
        candidates.append(fpath)

    deleted = 0
    for fpath in candidates:
        await _forget_semantic_file_state(db, path=fpath)
        cur = await db.execute(
            "SELECT id FROM documents WHERE file_path = ? AND workspace_id = ?",
            [fpath, workspace_id],
        )
        row = await cur.fetchone()
        if row is None:
            continue
        doc_id = row["id"]

        try:
            chunk_cur = await db.execute(
                "SELECT rowid FROM chunks WHERE doc_id = ?", [str(doc_id)]
            )
            chunk_rowids = [int(r["rowid"]) for r in await chunk_cur.fetchall()]
            for chunk_rowid in chunk_rowids:
                try:
                    await vec_db.execute(
                        "DELETE FROM vec_chunks WHERE chunk_rowid = ?", [chunk_rowid]
                    )
                except aiosqlite.OperationalError:
                    break
            await db.execute("DELETE FROM chunks WHERE doc_id = ?", [str(doc_id)])
        except aiosqlite.OperationalError as exc:
            logger.warning("delta chunk delete degraded (doc_id=%s): %s", doc_id, exc)

        try:
            await vec_db.execute("DELETE FROM vec_documents WHERE doc_id = ?", [doc_id])
        except aiosqlite.OperationalError as exc:
            logger.warning("delta vec_documents delete degraded (doc_id=%s): %s", doc_id, exc)
        try:
            await db.execute("DELETE FROM documents_fts WHERE rowid = ?", [doc_id])
        except aiosqlite.OperationalError as exc:
            logger.warning("delta documents_fts delete degraded (doc_id=%s): %s", doc_id, exc)

        await db.execute("DELETE FROM documents WHERE id = ?", [doc_id])
        deleted += 1

    await db.commit()
    if db is not vec_db:
        await vec_db.commit()

    return {"deleted": deleted, "skipped": len(candidates) - deleted, "total": len(candidates)}


async def _reindex_file_paths(
    workspace_id: str,
    db: aiosqlite.Connection,
    vec_db: aiosqlite.Connection,
    paths: Sequence[str | Path],
) -> dict:
    """Embed only the provided markdown file paths.

    This is the delta path used by the hosted watcher. It intentionally mirrors
    the `_reindex_files` write logic while avoiding a full project scan.
    """
    from core.api.routers.projects import PROJECT_DIRS
    from core.api.services.embedding_service import (
        EmbeddingInputTooLargeError,
        chunking_enabled,
        content_hash,
        embed_texts,
        persist_prose_chunks,
        serialize_f32,
    )

    seen: set[Path] = set()
    items: list[tuple[str, str, str, str, str, str, str]] = []
    skipped_entries: list[dict[str, str]] = []
    for raw_path in sorted(paths, key=lambda p: str(p)):
        path = Path(raw_path).expanduser()
        if path in seen:
            continue
        seen.add(path)
        item, skipped = _file_reindex_item_or_skip_from_path(path, PROJECT_DIRS)
        if skipped is not None:
            skipped_entries.append(skipped)
            continue
        if item is None:
            continue
        slug, fpath, title, text, doc_type, source_sha256 = item
        embed_text = text
        if len(text.encode("utf-8")) > FILE_LARGE_BYTES:
            embed_text = "\n".join(
                filter(None, [title, text[:FILE_LARGE_EMBED_CHARS]])
            )
        items.append(
            (slug, fpath, title, text, embed_text, doc_type, source_sha256)
        )

    # Filter unchanged + owner-confidential (RBAC F4: skip keyed on file_meta,
    # never the frontmatter — a stripped marker must not re-index the file)
    from core.api.services.confidential_files import file_path_confidential

    to_embed: list[tuple[str, str, str, str, str, str, str, str]] = []
    for slug, fpath, title, text, embed_text, doc_type, source_sha256 in items:
        if await file_path_confidential(
            db, slug, fpath, workspace_id=workspace_id
        ):
            skipped_entries.append({"file": fpath, "reason": "confidential"})
            continue
        h = content_hash(text)
        if await _unchanged_and_embedded(db, vec_db, fpath, h):
            await _record_semantic_file_state(
                db, path=fpath, sha256=source_sha256
            )
            continue
        to_embed.append(
            (slug, fpath, title, text, embed_text, doc_type, h, source_sha256)
        )

    await db.commit()  # Release read locks before embedding work

    if not to_embed:
        result = {
            "indexed": 0,
            "skipped": len(items) + len(skipped_entries),
            "total": len(items) + len(skipped_entries),
        }
        if skipped_entries:
            result["skipped_entries"] = skipped_entries
        return result

    indexed = 0
    terminal_skipped_entries: list[dict[str, str]] = []

    for start in range(0, len(to_embed), FILE_REINDEX_EMBED_BATCH_SIZE):
        batch = to_embed[start : start + FILE_REINDEX_EMBED_BATCH_SIZE]
        batch_texts = [embed_text for _, _, _, _, embed_text, _, _, _ in batch]
        try:
            embeddings = await embed_texts(batch_texts, input_type="document")
            embedded_items = list(zip(batch, embeddings))
        except EmbeddingInputTooLargeError:
            # The sidecar rejects a whole request, so isolate the offending item
            # without discarding the other document in this bounded batch.
            embedded_items = []
            for item in batch:
                (
                    slug,
                    fpath,
                    title,
                    text,
                    embed_text,
                    doc_type,
                    h,
                    source_sha256,
                ) = item
                try:
                    embedding = (
                        await embed_texts([embed_text], input_type="document")
                    )[0]
                except EmbeddingInputTooLargeError:
                    # Name the culprit in the journal, not only in the result
                    # dict (which aggregating callers historically dropped).
                    size = len(embed_text.encode("utf-8"))
                    logger.warning(
                        "document embedding exceeds sidecar limit, skipping: "
                        "%s (%d bytes)",
                        fpath,
                        size,
                    )
                    terminal_skipped_entries.append(
                        {
                            "file": fpath,
                            "reason": "embedding_input_too_large",
                            "bytes": str(size),
                        }
                    )
                    continue
                embedded_items.append(
                    (
                        (
                            slug,
                            fpath,
                            title,
                            text,
                            embed_text,
                            doc_type,
                            h,
                            source_sha256,
                        ),
                        embedding,
                    )
                )

        for (
            slug,
            fpath,
            title,
            text,
            _embed_text,
            doc_type,
            h,
            source_sha256,
        ), embedding in embedded_items:
            await db.execute(
                """INSERT INTO documents (file_path, project, workspace_id, doc_type, doc_title, content_hash)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(file_path) DO UPDATE SET
                     content_hash = excluded.content_hash,
                     project = excluded.project,
                     workspace_id = excluded.workspace_id,
                     doc_type = excluded.doc_type,
                     doc_title = excluded.doc_title""",
                [fpath, slug, workspace_id, doc_type, title, h],
            )
            cur2 = await db.execute(
                "SELECT id FROM documents WHERE file_path = ?", [fpath]
            )
            doc_row = await cur2.fetchone()
            doc_id = doc_row["id"]
            await _refresh_delta_documents_fts_row(
                db, doc_id=doc_id, title=title, content=text
            )

            await vec_db.execute("DELETE FROM vec_documents WHERE doc_id = ?", [doc_id])
            await vec_db.execute(
                "INSERT INTO vec_documents (doc_id, embedding) VALUES (?, ?)",
                [doc_id, serialize_f32(embedding)],
            )
            # A-span (Phase 2, gap S2): chunks sidecar stays in sync on reindex.
            if chunking_enabled():
                await persist_prose_chunks(
                    doc_id=str(doc_id), content=text, db=db, vec_db=vec_db
                )
            await _record_semantic_file_state(
                db, path=fpath, sha256=source_sha256
            )
            indexed += 1

    await db.commit()
    if db is not vec_db:
        await vec_db.commit()

    unchanged = len(items) - indexed
    result = {
        "indexed": indexed,
        "skipped": unchanged + len(skipped_entries),
        "total": len(items) + len(skipped_entries),
    }
    all_skipped_entries = skipped_entries + terminal_skipped_entries
    if all_skipped_entries:
        result["skipped_entries"] = all_skipped_entries
    return result


async def _reindex_files(
    workspace_id: str, db: aiosqlite.Connection, vec_db: aiosqlite.Connection
) -> dict:
    """Batch-embed all docs/**/*.md from filesystem (audits, solutions, brainstorms, plans, etc.)."""
    from core.api.routers.projects import PROJECT_DIRS

    paths: list[Path] = []
    for base in PROJECT_DIRS:
        if not base.exists():
            continue
        for project_dir in sorted(base.iterdir()):
            if not project_dir.is_dir() or project_dir.is_symlink():
                continue
            for f in sorted(project_dir.rglob("*.md")):
                if f.is_symlink():
                    continue
                try:
                    rel = f.relative_to(project_dir)
                except ValueError:
                    continue
                if any(part.startswith(".") for part in rel.parts[:-1]):
                    continue
                if rel.parts[:1] == ("memory",) and f.name.startswith("handoff-"):
                    continue
                paths.append(f)

    indexed = 0
    skipped = 0
    total = 0
    batch_size = 64
    for i in range(0, len(paths), batch_size):
        result = await _reindex_file_paths(
            workspace_id,
            db,
            vec_db,
            paths[i : i + batch_size],
        )
        indexed += int(result.get("indexed", 0))
        skipped += int(result.get("skipped", 0))
        total += int(result.get("total", 0))

        if i + batch_size < len(paths):
            await asyncio.sleep(20)

    return {"indexed": indexed, "skipped": skipped, "total": total}


async def _reindex_handoffs(
    workspace_id: str, db: aiosqlite.Connection, vec_db: aiosqlite.Connection
) -> dict:
    """Batch-embed handoff files from filesystem (memory/handoff-*.md)."""
    from core.api.routers.projects import PROJECT_DIRS
    from core.api.services.embedding_service import (
        chunking_enabled,
        content_hash,
        embed_texts,
        persist_prose_chunks,
        refresh_documents_fts_row,
        serialize_f32,
    )

    # Scan all projects for handoffs
    items: list[tuple[str, str, str, str]] = []  # (slug, file_path, title, content)
    for base in PROJECT_DIRS:
        if not base.exists():
            continue
        for project_dir in sorted(base.iterdir()):
            if not project_dir.is_dir() or project_dir.is_symlink():
                continue
            slug = project_dir.name
            memory_dir = project_dir / "memory"
            if not memory_dir.is_dir():
                continue
            for f in sorted(memory_dir.glob("handoff-*.md")):
                if f.is_symlink() or f.stat().st_size > 500_000:
                    continue
                try:
                    text = f.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                title = f.stem.replace("-", " ").strip()
                items.append((slug, str(f), title, text))

    # Filter unchanged
    to_embed: list[tuple[str, str, str, str, str]] = []
    for slug, fpath, title, text in items:
        h = content_hash(text)
        if await _unchanged_and_embedded(db, vec_db, fpath, h):
            continue
        to_embed.append((slug, fpath, title, text, h))

    await db.commit()  # Release read locks before API call

    if not to_embed:
        return {"indexed": 0, "skipped": len(items), "total": len(items)}

    # Batch embed
    indexed = 0
    batch_size = 64
    for i in range(0, len(to_embed), batch_size):
        batch = to_embed[i : i + batch_size]
        texts = [text for _, _, _, text, _ in batch]
        embeddings = await embed_texts(texts, input_type="document")

        for (slug, fpath, title, text, h), embedding in zip(batch, embeddings):
            await db.execute(
                """INSERT INTO documents (file_path, project, workspace_id, doc_type, doc_title, content_hash)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(file_path) DO UPDATE SET
                     content_hash = excluded.content_hash,
                     project = excluded.project,
                     workspace_id = excluded.workspace_id,
                     doc_type = excluded.doc_type,
                     doc_title = excluded.doc_title""",
                [fpath, slug, workspace_id, "handoff", title, h],
            )
            cur2 = await db.execute(
                "SELECT id FROM documents WHERE file_path = ?", [fpath]
            )
            doc_row = await cur2.fetchone()
            doc_id = doc_row["id"]
            # B-fix (MARVIS_FTS_BODIES): repair the trigger-degraded FTS row.
            await refresh_documents_fts_row(db, doc_id=doc_id, title=title, content=text)

            await vec_db.execute("DELETE FROM vec_documents WHERE doc_id = ?", [doc_id])
            await vec_db.execute(
                "INSERT INTO vec_documents (doc_id, embedding) VALUES (?, ?)",
                [doc_id, serialize_f32(embedding)],
            )
            # A-span (Phase 2, gap S2): chunks sidecar stays in sync on reindex.
            if chunking_enabled():
                await persist_prose_chunks(
                    doc_id=str(doc_id), content=text, db=db, vec_db=vec_db
                )
            indexed += 1

        await db.commit()
        if db is not vec_db:
            await vec_db.commit()

        if i + batch_size < len(to_embed):
            await asyncio.sleep(20)

    return {"indexed": indexed, "total": len(items)}


async def _reindex_learnings(
    workspace_id: str, db: aiosqlite.Connection, vec_db: aiosqlite.Connection
) -> dict:
    """Batch-embed learnings from DB (title + description + prevention)."""
    from core.api.services.embedding_service import (
        content_hash,
        embed_texts,
        refresh_documents_fts_row,
        serialize_f32,
    )

    cur = await db.execute(
        "SELECT id, title, description, prevention, category, severity, project "
        "FROM learnings WHERE workspace_id = ?",
        [workspace_id],
    )
    rows = await cur.fetchall()

    to_embed: list[tuple] = []  # (row_dict, content, hash, file_path)
    for row in rows:
        content = "\n".join(
            filter(
                None,
                [
                    row["title"],
                    row["description"],
                    f"Prevention: {row['prevention']}" if row["prevention"] else None,
                    f"Category: {row['category']}",
                    f"Severity: {row['severity']}",
                ],
            )
        )
        h = content_hash(content)
        file_path = f"learning:{row['id']}"
        if await _unchanged_and_embedded(db, vec_db, file_path, h):
            continue
        to_embed.append((dict(row), content, h, file_path))

    await db.commit()

    if not to_embed:
        return {"indexed": 0, "skipped": len(rows), "total": len(rows)}

    indexed = 0
    batch_size = 64
    for i in range(0, len(to_embed), batch_size):
        batch = to_embed[i : i + batch_size]
        texts = [content for _, content, _, _ in batch]
        embeddings = await embed_texts(texts, input_type="document")

        for (row_d, content, h, file_path), embedding in zip(batch, embeddings):
            await db.execute(
                """INSERT INTO documents (file_path, project, workspace_id, doc_type, doc_title, content_hash)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(file_path) DO UPDATE SET
                     content_hash = excluded.content_hash,
                     project = excluded.project,
                     workspace_id = excluded.workspace_id,
                     doc_type = excluded.doc_type,
                     doc_title = excluded.doc_title""",
                [
                    file_path,
                    row_d["project"] or "",
                    workspace_id,
                    "learning",
                    row_d["title"],
                    h,
                ],
            )
            cur3 = await db.execute(
                "SELECT id FROM documents WHERE file_path = ?", [file_path]
            )
            doc_row = await cur3.fetchone()
            doc_id = doc_row["id"]
            # B-fix (MARVIS_FTS_BODIES): repair the trigger-degraded FTS row.
            await refresh_documents_fts_row(
                db, doc_id=doc_id, title=row_d["title"], content=content
            )

            await vec_db.execute("DELETE FROM vec_documents WHERE doc_id = ?", [doc_id])
            await vec_db.execute(
                "INSERT INTO vec_documents (doc_id, embedding) VALUES (?, ?)",
                [doc_id, serialize_f32(embedding)],
            )
            indexed += 1

        await db.commit()
        if db is not vec_db:
            await vec_db.commit()

        if i + batch_size < len(to_embed):
            await asyncio.sleep(20)

    return {"indexed": indexed, "total": len(rows)}


async def _reindex_inbox_items(
    workspace_id: str, db: aiosqlite.Connection, vec_db: aiosqlite.Connection
) -> dict:
    """Batch-embed inbox items (title + snippet). Skips auto_ignored items."""
    from core.api.services.embedding_service import (
        content_hash,
        embed_texts,
        refresh_documents_fts_row,
        serialize_f32,
    )

    cur = await db.execute(
        "SELECT id, title, content, source, status "
        "FROM inbox_items "
        "WHERE workspace_id = ? AND status != 'auto_ignored'",
        [workspace_id],
    )
    rows = await cur.fetchall()

    to_embed: list[tuple] = []  # (row_dict, content, hash, file_path)
    for row in rows:
        # Build embeddable text from title + truncated content snippet
        snippet = (row["content"] or "")[:500]
        content = "\n".join(filter(None, [row["title"], snippet]))
        if not content.strip():
            continue
        h = content_hash(content)
        file_path = f"inbox_item:{row['id']}"
        if await _unchanged_and_embedded(db, vec_db, file_path, h):
            continue
        to_embed.append((dict(row), content, h, file_path))

    await db.commit()

    if not to_embed:
        return {"indexed": 0, "skipped": len(rows), "total": len(rows)}

    indexed = 0
    batch_size = 64
    for i in range(0, len(to_embed), batch_size):
        batch = to_embed[i : i + batch_size]
        texts = [content for _, content, _, _ in batch]
        embeddings = await embed_texts(texts, input_type="document")

        for (row_d, content, h, file_path), embedding in zip(batch, embeddings):
            await db.execute(
                """INSERT INTO documents (file_path, project, workspace_id, doc_type, doc_title, content_hash)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(file_path) DO UPDATE SET
                     content_hash = excluded.content_hash,
                     project = excluded.project,
                     workspace_id = excluded.workspace_id,
                     doc_type = excluded.doc_type,
                     doc_title = excluded.doc_title""",
                [file_path, "", workspace_id, "inbox_item", row_d["title"] or "", h],
            )
            cur3 = await db.execute(
                "SELECT id FROM documents WHERE file_path = ?", [file_path]
            )
            doc_row = await cur3.fetchone()
            doc_id = doc_row["id"]
            # B-fix (MARVIS_FTS_BODIES): the FULL body goes to the lexical
            # index (the embed used a 500-char snippet for vector quality).
            await refresh_documents_fts_row(
                db,
                doc_id=doc_id,
                title=row_d["title"] or "",
                content="\n".join(
                    filter(None, [row_d["title"], row_d["content"] or ""])
                ),
            )

            await vec_db.execute("DELETE FROM vec_documents WHERE doc_id = ?", [doc_id])
            await vec_db.execute(
                "INSERT INTO vec_documents (doc_id, embedding) VALUES (?, ?)",
                [doc_id, serialize_f32(embedding)],
            )
            indexed += 1

        await db.commit()
        if db is not vec_db:
            await vec_db.commit()

        if i + batch_size < len(to_embed):
            await asyncio.sleep(20)

    return {"indexed": indexed, "total": len(rows)}


async def _reindex_audits(
    workspace_id: str, db: aiosqlite.Connection, vec_db: aiosqlite.Connection
) -> dict:
    """Batch-embed audit files from filesystem (docs/audits/*.md)."""
    from core.api.routers.projects import PROJECT_DIRS
    from core.api.services.embedding_service import (
        chunking_enabled,
        content_hash,
        embed_texts,
        persist_prose_chunks,
        refresh_documents_fts_row,
        serialize_f32,
    )

    items: list[tuple[str, str, str, str]] = []  # (slug, file_path, title, content)
    for base in PROJECT_DIRS:
        if not base.exists():
            continue
        for project_dir in sorted(base.iterdir()):
            if not project_dir.is_dir() or project_dir.is_symlink():
                continue
            slug = project_dir.name
            audits_dir = project_dir / "docs" / "audits"
            if not audits_dir.is_dir():
                continue
            for f in sorted(audits_dir.rglob("*.md")):
                if f.is_symlink() or f.stat().st_size > 500_000:
                    continue
                try:
                    text = f.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                title = f.stem.replace("-", " ").strip()
                items.append((slug, str(f), title, text))

    # Filter unchanged
    to_embed: list[tuple[str, str, str, str, str]] = []
    for slug, fpath, title, text in items:
        h = content_hash(text)
        if await _unchanged_and_embedded(db, vec_db, fpath, h):
            continue
        to_embed.append((slug, fpath, title, text, h))

    await db.commit()

    if not to_embed:
        return {"indexed": 0, "skipped": len(items), "total": len(items)}

    indexed = 0
    batch_size = 64
    for i in range(0, len(to_embed), batch_size):
        batch = to_embed[i : i + batch_size]
        texts = [text for _, _, _, text, _ in batch]
        embeddings = await embed_texts(texts, input_type="document")

        for (slug, fpath, title, text, h), embedding in zip(batch, embeddings):
            await db.execute(
                """INSERT INTO documents (file_path, project, workspace_id, doc_type, doc_title, content_hash)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(file_path) DO UPDATE SET
                     content_hash = excluded.content_hash,
                     project = excluded.project,
                     workspace_id = excluded.workspace_id,
                     doc_type = excluded.doc_type,
                     doc_title = excluded.doc_title""",
                [fpath, slug, workspace_id, "audit", title, h],
            )
            cur2 = await db.execute(
                "SELECT id FROM documents WHERE file_path = ?", [fpath]
            )
            doc_row = await cur2.fetchone()
            doc_id = doc_row["id"]
            # B-fix (MARVIS_FTS_BODIES): repair the trigger-degraded FTS row.
            await refresh_documents_fts_row(db, doc_id=doc_id, title=title, content=text)

            await vec_db.execute("DELETE FROM vec_documents WHERE doc_id = ?", [doc_id])
            await vec_db.execute(
                "INSERT INTO vec_documents (doc_id, embedding) VALUES (?, ?)",
                [doc_id, serialize_f32(embedding)],
            )
            # A-span (Phase 2, gap S2): chunks sidecar stays in sync on reindex.
            if chunking_enabled():
                await persist_prose_chunks(
                    doc_id=str(doc_id), content=text, db=db, vec_db=vec_db
                )
            indexed += 1

        await db.commit()
        if db is not vec_db:
            await vec_db.commit()

        if i + batch_size < len(to_embed):
            await asyncio.sleep(20)

    return {"indexed": indexed, "total": len(items)}
