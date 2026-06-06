# v2.4.0 - 2026-04-12 - Add learning, inbox_item, audit doc_types to search grouping
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import hashlib
import logging
import os
import struct
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Literal, cast

import aiosqlite

from core.api.services._fts import fts5_safe_query

logger = logging.getLogger(__name__)

DIMENSIONS = 512
GRANITE_NATIVE_DIMENSIONS = 384
DEFAULT_SEARCH_RRF_K = 60
DEFAULT_SEARCH_SALIENCE_WEIGHT = 0.3
DEFAULT_SEARCH_SCORE_THRESHOLD = 0.6
BM25_FETCH_LIMIT = 50
# Canonical, backend-agnostic mode names. "remote" selects the optional remote
# backend (a deploy-only module); "granite_local" the in-process engine;
# "granite_remote" the Phase 2 sidecar; "dual" runs the local validator and
# returns the remote vectors. Provider-specific aliases (resolved to one of
# these) live in the remote backend module, never here.
EmbeddingMode = Literal["remote", "granite_local", "granite_remote", "dual"]
_VALID_EMBEDDING_MODES = {"remote", "granite_local", "granite_remote", "dual"}

# The local engine remains lazy and loads the onnxruntime session + tokenizer
# (torch-free) on first use. The optional remote backend (when present) owns its
# own client lifecycle inside its module.
_granite_client = None

# F2: serialize local-backend model runs process-wide. The ONNX session.run executes
# OUTSIDE the per-client encode lock, so concurrent reindex + search + fan-out
# create-hooks would each allocate the bounded batch budget → N × budget peak. A
# single-permit semaphore makes the F2 bound hold under real concurrency (cost is
# near-zero: the CPU graph run is already core-saturated and serial in practice).
_granite_run_semaphore: asyncio.Semaphore | None = None


def _local_run_semaphore() -> asyncio.Semaphore:
    global _granite_run_semaphore
    if _granite_run_semaphore is None:
        _granite_run_semaphore = asyncio.Semaphore(1)
    return _granite_run_semaphore


@dataclass(frozen=True, slots=True)
class _EmbeddingHit:
    doc_id: int
    distance: float


@dataclass(frozen=True, slots=True)
class _Bm25Hit:
    doc_id: int
    score: float


@dataclass(frozen=True, slots=True)
class _HybridRankScore:
    doc_id: int
    score: float
    rrf_score: float
    rank_embedding: int | None
    rank_bm25: int | None


def _remote_backend():
    """Return the optional remote embedding backend module, or None if absent.

    Cached per-process. A clean OSS clone ships no remote backend → None →
    local-only. A deploy that includes it (with its key configured) routes here.
    """
    from core.api.services.embedding_backends import load_remote_backend

    return load_remote_backend()


def init_embedding_client() -> None:
    """Called from lifespan. Initializes whichever backend the mode resolves to."""
    mode = _embedding_mode()
    if mode in {"remote", "dual"}:
        backend = _remote_backend()
        if backend is not None:
            backend.init_client()
    if mode in {"granite_local", "dual"}:
        client = _get_granite_client()
        logger.info(
            "Granite local embedding client configured (lazy load, model=%s, device=%s)",
            client.active_model_name,
            client.device,
        )
    if mode == "granite_remote":
        logger.info("Granite remote embedding mode selected; Phase 2 sidecar pending")


def is_available() -> bool:
    mode = _embedding_mode()
    if mode in {"remote", "dual"}:
        backend = _remote_backend()
        return backend is not None and backend.client_ready()
    if mode == "granite_local":
        # F1: honest readiness. The client's is_available() actually attempts the
        # load and records any error, unlike can_attempt_load which stays True
        # until a crash is recorded → it reported "available" before the model had
        # ever loaded, letting a dead retriever degrade to keyword-only silently.
        return _get_granite_client().is_available()
    return False


# F2/U6: a background or opportunistic index build must never OOM a self-hoster's
# laptop. The per-doc/token-budget batcher bounds peak embed memory, but the model
# load + a long doc can still spike on a low-free-RAM machine, so an auto-build
# (lazy first-search self-heal, opportunistic upkeep) first checks this floor.
# Tunable via MARVIS_INDEX_MIN_AVAILABLE_GB; psutil is optional (mirrors doctor).
BACKGROUND_INDEX_MIN_AVAILABLE_GB: float = float(
    os.environ.get("MARVIS_INDEX_MIN_AVAILABLE_GB", "1.5")
)


def background_index_ram_ok() -> tuple[bool, str]:
    """Whether there is enough free RAM to safely start a background index build.

    Returns ``(ok, reason)``. psutil is an optional dependency: when it is absent
    or the probe fails we do NOT block (mirroring ``marvis doctor``) — the F2
    token-budget batcher is the second line of defence against OOM.
    """
    try:
        import psutil
    except Exception:  # noqa: BLE001 — optional dep
        return True, "psutil unavailable — RAM gate skipped"
    try:
        avail_gb = psutil.virtual_memory().available / (1024**3)
    except Exception as exc:  # noqa: BLE001
        return True, f"RAM probe failed ({exc}) — gate skipped"
    if avail_gb < BACKGROUND_INDEX_MIN_AVAILABLE_GB:
        return False, (
            f"available RAM {avail_gb:.1f} GB is below the background-index floor "
            f"{BACKGROUND_INDEX_MIN_AVAILABLE_GB} GB — deferring the build"
        )
    return True, f"available RAM {avail_gb:.1f} GB"


def load_error_message() -> str | None:
    """Sanitized-at-surface read of the local backend's last load error.

    Returned ONLY so the caller can classify the failure into a coarse enum — the
    raw string is never surfaced to clients (it can embed a path/backend name; OSS
    no-leak rule). Remote/dual backends own their own readiness → None there.
    """
    if _embedding_mode() == "granite_local":
        return _get_granite_client().load_error_message
    return None


async def embed_texts(
    texts: list[str],
    input_type: str = "document",
    batch_size: int = 16,
) -> list[list[float]]:
    """Embed texts using the configured backend.

    Public callers keep receiving 512-dimensional vectors because sqlite-vec's
    vec_documents table is fixed at float[512].
    """
    mode = _embedding_mode()
    if mode == "remote":
        return await _embed_remote_texts(texts, input_type=input_type)
    if mode == "granite_local":
        return await _embed_granite_texts(
            texts,
            input_type=input_type,
            batch_size=batch_size,
        )
    if mode == "dual":
        remote_embeddings = await _embed_remote_texts(texts, input_type=input_type)
        try:
            await _embed_granite_texts(
                texts,
                input_type=input_type,
                batch_size=batch_size,
            )
        except Exception:
            logger.exception("Granite dual-mode validation embedding failed")
        return remote_embeddings
    raise NotImplementedError("Phase 2 sidecar Docker mode")


async def _embed_remote_texts(
    texts: list[str],
    input_type: str = "document",
) -> list[list[float]]:
    """Embed texts via the optional remote backend (deploy-only module)."""
    backend = _remote_backend()
    if backend is None:
        raise RuntimeError("Remote embedding backend not installed")
    return await backend.embed(texts, input_type=input_type)


async def _embed_granite_texts(
    texts: list[str],
    input_type: str,
    batch_size: int,
) -> list[list[float]]:
    client = _get_granite_client()
    # F2: one model run at a time across the process (see _local_run_semaphore).
    async with _local_run_semaphore():
        embeddings = await asyncio.to_thread(
            client.embed_texts,
            texts,
            input_type=input_type,
            batch_size=batch_size,
        )
    return [_coerce_dimensions_for_vec_documents(embedding) for embedding in embeddings]


def _embedding_mode() -> EmbeddingMode:
    backend = _remote_backend()
    raw = os.environ.get("EMBEDDING_MODE")
    if raw is None or not raw.strip():
        # No explicit mode: resolve on the remote backend being present AND
        # configured (its key set), never on deploy_mode. A clean OSS install
        # has no remote backend module -> granite_local (in-process engine).
        # Prod ships the remote backend with its key configured -> "remote",
        # behavior-identical to the pre-carve-out resolution.
        if backend is not None and backend.is_configured():
            return "remote"
        return "granite_local"
    mode = raw.strip().lower()
    # Provider-specific EMBEDDING_MODE aliases (e.g. legacy explicit values) are
    # declared by the remote backend so the alias literals never live in shipped
    # code. They map to the canonical "remote" / "dual" names here.
    if backend is not None:
        if mode in getattr(backend, "DUAL_MODE_ALIASES", frozenset()):
            return "dual"
        if mode in getattr(backend, "MODE_ALIASES", frozenset()):
            return "remote"
    if mode not in _VALID_EMBEDDING_MODES:
        logger.warning(
            "Unknown EMBEDDING_MODE=%r; falling back to granite_local", mode
        )
        return "granite_local"
    return cast(EmbeddingMode, mode)


def _search_rrf_k() -> int:
    return max(_int_from_env("SEARCH_RRF_K", DEFAULT_SEARCH_RRF_K), 1)


def _search_salience_weight() -> float:
    return max(_float_from_env("SEARCH_SALIENCE_WEIGHT", DEFAULT_SEARCH_SALIENCE_WEIGHT), 0.0)


def _search_score_threshold() -> float:
    return _float_from_env("SEARCH_SCORE_THRESHOLD", DEFAULT_SEARCH_SCORE_THRESHOLD)


def _search_bm25_enabled() -> bool:
    raw = os.environ.get("SEARCH_BM25_ENABLED", "true").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def _int_from_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("Invalid %s=%r; using %d", name, raw, default)
        return default


def _float_from_env(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning("Invalid %s=%r; using %.3f", name, raw, default)
        return default


def _get_granite_client():
    global _granite_client
    if _granite_client is None:
        from core.api.services.embedding_internal import GraniteEmbeddingClient

        _granite_client = GraniteEmbeddingClient(
            device=os.environ.get("EMBEDDING_DEVICE", "cpu"),
        )
    return _granite_client


def _coerce_dimensions_for_vec_documents(vec: list[float]) -> list[float]:
    if len(vec) == DIMENSIONS:
        return vec
    if len(vec) > DIMENSIONS:
        return vec[:DIMENSIONS]

    # W1 pragmatism: vec_documents is hardcoded as float[512]. Granite-97m emits
    # native 384-dimensional, L2-normalized vectors; zero-padding preserves their
    # geometry without a migration. Phase 2 can introduce a
    # vec_documents_granite float[384] shadow table and route per backend.
    return [*vec, *([0.0] * (DIMENSIONS - len(vec)))]


def _empty_grouped_results() -> dict[str, list[dict]]:
    return {
        "task": [],
        "project": [],
        "file": [],
        "handoff": [],
        "learning": [],
        "inbox_item": [],
        "audit": [],
    }


def _normalize_salience(value: object) -> float:
    try:
        salience = float(value) if value is not None else 0.5
    except (TypeError, ValueError):
        salience = 0.5
    return min(max(salience, 0.0), 1.0)


def _reciprocal_rank_score(
    rank_embedding: int | None,
    rank_bm25: int | None,
    *,
    rrf_k: int,
) -> float:
    score = 0.0
    if rank_embedding is not None:
        score += 1.0 / (rrf_k + rank_embedding)
    if rank_bm25 is not None:
        score += 1.0 / (rrf_k + rank_bm25)
    return score


def _public_hybrid_score(
    rrf_score: float,
    salience: float,
    *,
    salience_weight: float,
    max_rrf_score: float,
) -> float:
    """Map tiny RRF values onto the public 0-ish relevance scale before boost."""
    normalized_rrf = rrf_score / max_rrf_score if max_rrf_score > 0 else 0.0
    return normalized_rrf + salience_weight * _normalize_salience(salience)


def _embedding_only_score(
    distance: float,
    salience: float,
    *,
    salience_weight: float,
) -> float:
    similarity = 1.0 - (distance / 2.0)
    similarity = min(max(similarity, 0.0), 1.0)
    semantic_weight = max(1.0 - salience_weight, 0.0)
    return similarity * semantic_weight + _normalize_salience(salience) * salience_weight


def _fuse_ranked_documents(
    embedding_hits: Sequence[_EmbeddingHit],
    bm25_hits: Sequence[_Bm25Hit],
    salience_by_doc_id: Mapping[int, float],
    *,
    rrf_k: int,
    salience_weight: float,
    threshold: float,
    allowed_doc_ids: set[int] | None = None,
) -> list[_HybridRankScore]:
    embedding_ranks = {hit.doc_id: rank for rank, hit in enumerate(embedding_hits, start=1)}
    bm25_ranks = {hit.doc_id: rank for rank, hit in enumerate(bm25_hits, start=1)}
    ordered_doc_ids = list(dict.fromkeys([*embedding_ranks.keys(), *bm25_ranks.keys()]))
    if allowed_doc_ids is not None:
        ordered_doc_ids = [doc_id for doc_id in ordered_doc_ids if doc_id in allowed_doc_ids]

    source_count = 1 + (1 if bm25_hits else 0)
    max_rrf_score = source_count * (1.0 / (rrf_k + 1))

    fused: list[_HybridRankScore] = []
    for doc_id in ordered_doc_ids:
        rank_embedding = embedding_ranks.get(doc_id)
        rank_bm25 = bm25_ranks.get(doc_id)
        rrf_score = _reciprocal_rank_score(
            rank_embedding,
            rank_bm25,
            rrf_k=rrf_k,
        )
        score = _public_hybrid_score(
            rrf_score,
            salience_by_doc_id.get(doc_id, 0.5),
            salience_weight=salience_weight,
            max_rrf_score=max_rrf_score,
        )
        if score < threshold:
            continue
        fused.append(
            _HybridRankScore(
                doc_id=doc_id,
                score=score,
                rrf_score=rrf_score,
                rank_embedding=rank_embedding,
                rank_bm25=rank_bm25,
            )
        )

    def _best_rank(item: _HybridRankScore) -> int:
        ranks = [r for r in (item.rank_embedding, item.rank_bm25) if r is not None]
        return min(ranks) if ranks else 10**9

    fused.sort(key=lambda item: (item.score, item.rrf_score, -_best_rank(item)), reverse=True)
    return fused


def serialize_f32(vec: list[float]) -> bytes:
    """Serialize float vector to binary format for sqlite-vec."""
    vec = _coerce_dimensions_for_vec_documents(vec)
    return struct.pack(f"{len(vec)}f", *vec)


def content_hash(text: str) -> str:
    """SHA-256 hash of content for change detection."""
    return hashlib.sha256(text.encode()).hexdigest()[:16]


# --- Track 2 #4: fixed prose chunking (flag MARVIS_CHUNKING, DEFAULT OFF) ----
# When OFF (the default), nothing below runs and every embed/search path is
# byte-for-byte the current behavior. The flag is read per-call (cheap) so it can
# be flipped without a process restart in a test/bench.
_CHUNKING_ENABLED_VALUES = {"1", "on", "true", "yes"}


def chunking_enabled() -> bool:
    """True only when MARVIS_CHUNKING is explicitly set to an ON value.

    DEFAULT OFF: env unset / any other value → False → the live whole-doc embed
    path is untouched. This is the single gate the wiring + the search-side
    aggregation both consult, so the feature is fully dormant by default.
    """
    raw = os.environ.get("MARVIS_CHUNKING")
    if raw is None:
        return False
    return raw.strip().lower() in _CHUNKING_ENABLED_VALUES


def _pack_chunk_vector(vec: Sequence[float]) -> bytes:
    """Pack a chunk vector as native little-endian float32 (the chunks BLOB).

    NOT the 512-padded vec0 layout (``serialize_f32``): the chunks sidecar stores
    the native 384-dim Granite vector raw, mirroring ``graph_node_code_embeddings``.
    """
    return struct.pack(f"<{len(vec)}f", *(float(x) for x in vec))


async def persist_prose_chunks(
    *,
    doc_id: str,
    content: str,
    db: aiosqlite.Connection,
) -> int:
    """Chunk a PROSE doc, embed the chunks, and upsert them into ``chunks``.

    No-op (returns 0) unless ``chunking_enabled()`` — so the default path never
    touches this table. PROSE ONLY: the caller is responsible for never invoking
    this on code (code is chunked per-symbol upstream in ``_index_source.py``).

    Per-chunk content-hash idempotency: a chunk whose ``content_hash`` already
    matches the stored row is skipped (re-embed only changed/new chunks); chunks
    that vanished (doc shrank) are deleted. The embedding GATHER happens before
    the write (the caller already holds the doc write lock pattern; this runs the
    embed via ``embed_texts`` which offloads off-lock).
    """
    if not chunking_enabled() or not is_available():
        return 0

    from core.api.services.chunking import chunk_prose

    tokenizer = _get_granite_client().tokenizer()
    chunks = chunk_prose(content, tokenizer)
    if not chunks:
        await db.execute("DELETE FROM chunks WHERE doc_id = ?", [doc_id])
        return 0

    # Existing chunk hashes for this doc → skip unchanged, delete vanished.
    cur = await db.execute(
        "SELECT chunk_id, content_hash FROM chunks WHERE doc_id = ?", [doc_id]
    )
    existing = {row[0]: row[1] for row in await cur.fetchall()}

    current_ids: set[str] = set()
    pending: list[tuple[str, int, object]] = []  # (chunk_id, idx, Chunk)
    for c in chunks:
        chunk_id = f"{doc_id}:{c.chunk_idx}"
        current_ids.add(chunk_id)
        if existing.get(chunk_id) == c.content_hash:
            continue  # unchanged → keep stored vector
        pending.append((chunk_id, c.chunk_idx, c))

    stale_ids = [cid for cid in existing if cid not in current_ids]
    if stale_ids:
        placeholders = ",".join("?" * len(stale_ids))
        await db.execute(
            f"DELETE FROM chunks WHERE chunk_id IN ({placeholders})", stale_ids
        )

    if not pending:
        return 0

    raw = content.encode("utf-8")
    texts = [raw[c.span_start : c.span_end].decode("utf-8", "replace") for _, _, c in pending]
    embeddings = await embed_texts(texts, input_type="document")
    for (chunk_id, idx, c), vec in zip(pending, embeddings):
        await db.execute(
            """INSERT INTO chunks
                 (chunk_id, doc_id, chunk_idx, span_start, span_end, content_hash, vector)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(chunk_id) DO UPDATE SET
                 doc_id = excluded.doc_id, chunk_idx = excluded.chunk_idx,
                 span_start = excluded.span_start, span_end = excluded.span_end,
                 content_hash = excluded.content_hash, vector = excluded.vector""",
            [
                chunk_id,
                doc_id,
                idx,
                c.span_start,
                c.span_end,
                c.content_hash,
                _pack_chunk_vector(vec),
            ],
        )
    return len(pending)


def aggregate_chunk_hits_to_docs(
    chunk_hits: Sequence[Mapping[str, object]],
    *,
    over_fetch: int = 5,
) -> list[dict]:
    """MAX-POOL chunk hits → one ranked row per doc (dormant under the flag).

    ``chunk_hits`` = retrieval rows each with ``doc_id``, ``chunk_id`` and a
    similarity ``score`` (already over-fetched K chunks so a doc isn't lost when
    its best chunk ranks 11th). Groups by ``doc_id``, keeps the BEST chunk score
    as the doc score (max-pool > sum: sum has a length bias where long docs win
    via many mediocre chunks), and carries that winning ``chunk_id`` as the #2
    citation anchor. Returns docs sorted by score DESC, ``doc_id`` ASC (the
    deterministic tie-break the eval harness pins).

    Pure + caller-gated: ``search`` only routes through here when ``chunks`` is
    populated AND ``chunking_enabled()``; otherwise default search is unchanged.
    ``over_fetch`` documents the recommended chunk→doc fan-in (K≈over_fetch×top-N)
    so callers size the chunk fetch — it does not truncate here.
    """
    best: dict[str, dict] = {}
    for hit in chunk_hits:
        doc_id = hit.get("doc_id")
        if doc_id is None:
            continue
        doc_key = str(doc_id)
        score = float(hit.get("score") or 0.0)
        prev = best.get(doc_key)
        if prev is None or score > prev["score"]:
            best[doc_key] = {
                "doc_id": doc_key,
                "score": score,
                "chunk_id": hit.get("chunk_id"),
            }
    return sorted(best.values(), key=lambda r: (-r["score"], r["doc_id"]))


async def upsert_document(
    *,
    file_path: str,
    project: str,
    workspace_id: str,
    doc_type: str,
    doc_title: str,
    content: str,
    db: aiosqlite.Connection,
    vec_db: aiosqlite.Connection,
) -> bool:
    """Embed and upsert a single document. Returns True if embedding was updated."""
    h = content_hash(content)
    cur = await db.execute(
        "SELECT id, content_hash FROM documents WHERE file_path = ?", [file_path]
    )
    row = await cur.fetchone()
    if row and row["content_hash"] == h:
        return False  # unchanged

    embeddings = await embed_texts([content], input_type="document")
    vec_bytes = serialize_f32(embeddings[0])

    await db.execute(
        """INSERT INTO documents (file_path, project, workspace_id, doc_type, doc_title, content_hash)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(file_path) DO UPDATE SET
             content_hash = excluded.content_hash,
             project = excluded.project,
             workspace_id = excluded.workspace_id,
             doc_type = excluded.doc_type,
             doc_title = excluded.doc_title""",
        [file_path, project, workspace_id, doc_type, doc_title, h],
    )
    cur2 = await db.execute("SELECT id FROM documents WHERE file_path = ?", [file_path])
    doc_row = await cur2.fetchone()
    doc_id = doc_row["id"]

    # vec0 virtual table does not support ON CONFLICT/UPSERT — DELETE + INSERT
    await vec_db.execute("DELETE FROM vec_documents WHERE doc_id = ?", [doc_id])
    await vec_db.execute(
        "INSERT INTO vec_documents (doc_id, embedding) VALUES (?, ?)",
        [doc_id, vec_bytes],
    )
    await db.commit()
    await vec_db.commit()
    return True


async def embed_task_document(
    *,
    task_id: str,
    title: str,
    project: str,
    status: str,
    workspace_id: str,
) -> None:
    """Embed a task into documents + vec_documents on the single-writer pool.

    The ONE fastapi-free implementation of the task auto-embed body. Both the HTTP
    surface (``routers.tasks._schedule_embed_task``) and the MCP surface
    (``mcp._adapter.mcp_schedule_embed``) call this — no fork. Callers schedule it
    fire-and-forget; this function owns the embed-then-write sequencing:

      1. GATHER (slow, ~10-15s for the remote backend / local model inference) the embedding OUTSIDE
         the write lock — building the content string and calling ``embed_texts``.
      2. WRITE (fast, <10ms) the documents row + vec_documents vector INSIDE one
         ``write_db`` acquisition. The single-writer ``asyncio.Lock`` is NOT
         reentrant, so we never hold it across the embed call (learning f83f5209).

    Writes go through ``write_db`` (the writer pool); the read pool is
    ``query_only=ON`` and would fail any INSERT (learning 6130bc49). No-ops if the
    embedder is unavailable — the caller is expected to gate on ``is_available()``
    too, but this guard keeps the helper safe to call directly.
    """
    if not is_available():
        return

    content = f"{title}\nStatus: {status}\nProject: {project}"
    # GATHER: embedding (remote backend HTTP / local model inference) OUTSIDE the write lock.
    h = content_hash(content)
    embeddings = await embed_texts([content], input_type="document")
    vec_bytes = serialize_f32(embeddings[0])

    # WRITE: DB operations INSIDE the write lock (fast batch).
    from core.api.db import ensure_vec_documents, write_db

    async with write_db(label="tasks.auto_embed") as db:
        if not await ensure_vec_documents(db):
            return

        cur = await db.execute(
            "SELECT id, content_hash FROM documents WHERE file_path = ?",
            [f"task:{task_id}"],
        )
        row = await cur.fetchone()
        if row and row["content_hash"] == h:
            return  # unchanged

        await db.execute(
            """INSERT INTO documents (file_path, project, workspace_id, doc_type, doc_title, content_hash)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(file_path) DO UPDATE SET
                 content_hash = excluded.content_hash, project = excluded.project,
                 workspace_id = excluded.workspace_id, doc_type = excluded.doc_type,
                 doc_title = excluded.doc_title""",
            [f"task:{task_id}", project, workspace_id, "task", title, h],
        )
        cur2 = await db.execute(
            "SELECT id FROM documents WHERE file_path = ?",
            [f"task:{task_id}"],
        )
        doc_row = await cur2.fetchone()
        doc_id = doc_row["id"]
        await db.execute("DELETE FROM vec_documents WHERE doc_id = ?", [doc_id])
        await db.execute(
            "INSERT INTO vec_documents (doc_id, embedding) VALUES (?, ?)",
            [doc_id, vec_bytes],
        )


async def embed_learning_document(
    *,
    learning_id: str,
    title: str,
    description: str,
    category: str,
    severity: str,
    prevention: str | None = None,
    project: str | None = None,
    workspace_id: str,
) -> None:
    """Embed a learning into documents + vec_documents on the single-writer pool.

    The learning twin of :func:`embed_task_document` — the ONE fastapi-free embed
    body for learnings, called by both the HTTP surface
    (``routers.learnings._schedule_embed_learning``, fire-and-forget) and the MCP
    surface (``mcp._adapter.mcp_embed_learning``, synchronous on a local backend). It
    owns the embed-then-write sequencing:

      1. GATHER (slow: model inference / remote backend HTTP) the embedding OUTSIDE the write
         lock — building the content string and calling ``embed_texts``.
      2. WRITE (fast, <10ms) the documents row + vec_documents vector INSIDE one
         ``write_db`` acquisition. The single-writer ``asyncio.Lock`` is NOT reentrant,
         so we never hold it across the embed call (learning f83f5209).

    The content mirrors ``use_cases.search._reindex_learnings`` (title + description +
    prevention + category + severity) so the on-write embedding is byte-identical to
    the on-reindex one — a later reindex never produces a different vector for the same
    learning. Writes go through ``write_db`` (the writer pool); the read pool is
    ``query_only=ON`` (learning 6130bc49). No-ops if the embedder is unavailable.
    """
    if not is_available():
        return

    content = "\n".join(
        filter(
            None,
            [
                title,
                description,
                f"Prevention: {prevention}" if prevention else None,
                f"Category: {category}",
                f"Severity: {severity}",
            ],
        )
    )
    file_path = f"learning:{learning_id}"
    h = content_hash(content)
    # GATHER: embedding (remote backend HTTP / local model inference) OUTSIDE the write lock.
    embeddings = await embed_texts([content], input_type="document")
    vec_bytes = serialize_f32(embeddings[0])

    # WRITE: DB operations INSIDE the write lock (fast batch).
    from core.api.db import ensure_vec_documents, write_db

    async with write_db(label="learnings.auto_embed") as db:
        if not await ensure_vec_documents(db):
            return

        cur = await db.execute(
            "SELECT id, content_hash FROM documents WHERE file_path = ?",
            [file_path],
        )
        row = await cur.fetchone()
        if row and row["content_hash"] == h:
            return  # unchanged

        await db.execute(
            """INSERT INTO documents (file_path, project, workspace_id, doc_type, doc_title, content_hash)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(file_path) DO UPDATE SET
                 content_hash = excluded.content_hash, project = excluded.project,
                 workspace_id = excluded.workspace_id, doc_type = excluded.doc_type,
                 doc_title = excluded.doc_title""",
            [file_path, project or "", workspace_id, "learning", title, h],
        )
        cur2 = await db.execute(
            "SELECT id FROM documents WHERE file_path = ?",
            [file_path],
        )
        doc_row = await cur2.fetchone()
        doc_id = doc_row["id"]
        await db.execute("DELETE FROM vec_documents WHERE doc_id = ?", [doc_id])
        await db.execute(
            "INSERT INTO vec_documents (doc_id, embedding) VALUES (?, ?)",
            [doc_id, vec_bytes],
        )


async def embed_project_document(
    *,
    slug: str,
    name: str,
    description: str,
    workspace_id: str,
) -> None:
    """Embed a project into documents + vec_documents on the single-writer pool.

    The project twin of :func:`embed_learning_document` — the ONE fastapi-free embed
    body for projects, called by the HTTP create surface
    (``routers.projects._schedule_embed_project``) fire-and-forget. It owns the
    embed-then-write sequencing:

      1. GATHER (slow: model inference / embedding backend HTTP) the embedding OUTSIDE
         the write lock — building the content string and calling ``embed_texts``.
      2. WRITE (fast, <10ms) the documents row + vec_documents vector INSIDE one
         ``write_db`` acquisition. The single-writer ``asyncio.Lock`` is NOT reentrant,
         so we never hold it across the embed call (learning f83f5209).

    The keying + content mirror ``use_cases.search._reindex_projects`` verbatim
    (``file_path = "project:{slug}"``, ``doc_type="project"``, ``doc_title=name``,
    ``project=slug``, content = ``"{slug}\\n{name}\\n{description}"``) so the on-write
    embedding is byte-identical to the on-reindex one — a later reindex never produces
    a different vector for the same project. Writes go through ``write_db`` (the writer
    pool); the read pool is ``query_only=ON`` (learning 6130bc49). No-ops if the
    embedder is unavailable.
    """
    if not is_available():
        return

    content = f"{slug}\n{name}\n{description}"
    file_path = f"project:{slug}"
    h = content_hash(content)
    # GATHER: embedding (embedding backend HTTP / local model inference) OUTSIDE the write lock.
    embeddings = await embed_texts([content], input_type="document")
    vec_bytes = serialize_f32(embeddings[0])

    # WRITE: DB operations INSIDE the write lock (fast batch).
    from core.api.db import ensure_vec_documents, write_db

    async with write_db(label="projects.auto_embed") as db:
        if not await ensure_vec_documents(db):
            return

        cur = await db.execute(
            "SELECT id, content_hash FROM documents WHERE file_path = ?",
            [file_path],
        )
        row = await cur.fetchone()
        if row and row["content_hash"] == h:
            return  # unchanged

        await db.execute(
            """INSERT INTO documents (file_path, project, workspace_id, doc_type, doc_title, content_hash)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(file_path) DO UPDATE SET
                 content_hash = excluded.content_hash, project = excluded.project,
                 workspace_id = excluded.workspace_id, doc_type = excluded.doc_type,
                 doc_title = excluded.doc_title""",
            [file_path, slug, workspace_id, "project", name, h],
        )
        cur2 = await db.execute(
            "SELECT id FROM documents WHERE file_path = ?",
            [file_path],
        )
        doc_row = await cur2.fetchone()
        doc_id = doc_row["id"]
        await db.execute("DELETE FROM vec_documents WHERE doc_id = ?", [doc_id])
        await db.execute(
            "INSERT INTO vec_documents (doc_id, embedding) VALUES (?, ?)",
            [doc_id, vec_bytes],
        )


async def embed_inbox_document(
    *,
    item_id: str,
    title: str,
    content: str | None,
    workspace_id: str,
) -> None:
    """Embed an inbox item into documents + vec_documents on the single-writer pool.

    The inbox twin of :func:`embed_learning_document` — the ONE fastapi-free embed
    body for inbox items, called by the create surface
    (``services.inbox._schedule_embed_inbox``) fire-and-forget. It owns the
    embed-then-write sequencing:

      1. GATHER (slow: model inference / embedding backend HTTP) the embedding OUTSIDE
         the write lock — building the content string and calling ``embed_texts``.
      2. WRITE (fast, <10ms) the documents row + vec_documents vector INSIDE one
         ``write_db`` acquisition. The single-writer ``asyncio.Lock`` is NOT reentrant,
         so we never hold it across the embed call (learning f83f5209).

    The keying + content mirror ``use_cases.search._reindex_inbox_items`` verbatim
    (``file_path = "inbox_item:{id}"``, ``doc_type="inbox_item"``, ``doc_title=title``,
    ``project=""``, content = ``"\\n".join(filter(None, [title, (content or "")[:500]]))``)
    so the on-write embedding is byte-identical to the on-reindex one — a later reindex
    never produces a different vector for the same item. If the content is empty (no
    title, no body) it no-ops, matching the reindex skip. Writes go through ``write_db``
    (the writer pool); the read pool is ``query_only=ON`` (learning 6130bc49). No-ops if
    the embedder is unavailable.
    """
    if not is_available():
        return

    snippet = (content or "")[:500]
    embed_content = "\n".join(filter(None, [title, snippet]))
    if not embed_content.strip():
        return  # nothing to embed (matches reindex skip)

    file_path = f"inbox_item:{item_id}"
    h = content_hash(embed_content)
    # GATHER: embedding (embedding backend HTTP / local model inference) OUTSIDE the write lock.
    embeddings = await embed_texts([embed_content], input_type="document")
    vec_bytes = serialize_f32(embeddings[0])

    # WRITE: DB operations INSIDE the write lock (fast batch).
    from core.api.db import ensure_vec_documents, write_db

    async with write_db(label="inbox.auto_embed") as db:
        if not await ensure_vec_documents(db):
            return

        cur = await db.execute(
            "SELECT id, content_hash FROM documents WHERE file_path = ?",
            [file_path],
        )
        row = await cur.fetchone()
        if row and row["content_hash"] == h:
            return  # unchanged

        await db.execute(
            """INSERT INTO documents (file_path, project, workspace_id, doc_type, doc_title, content_hash)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(file_path) DO UPDATE SET
                 content_hash = excluded.content_hash, project = excluded.project,
                 workspace_id = excluded.workspace_id, doc_type = excluded.doc_type,
                 doc_title = excluded.doc_title""",
            [file_path, "", workspace_id, "inbox_item", title or "", h],
        )
        cur2 = await db.execute(
            "SELECT id FROM documents WHERE file_path = ?",
            [file_path],
        )
        doc_row = await cur2.fetchone()
        doc_id = doc_row["id"]
        await db.execute("DELETE FROM vec_documents WHERE doc_id = ?", [doc_id])
        await db.execute(
            "INSERT INTO vec_documents (doc_id, embedding) VALUES (?, ?)",
            [doc_id, vec_bytes],
        )


async def embed_doc_document(
    *,
    file_path: str,
    title: str,
    content: str,
    project: str,
    workspace_id: str,
) -> None:
    """Embed a saved workflow doc into documents + vec_documents on the writer pool.

    The doc twin of :func:`embed_learning_document` — the ONE fastapi-free embed body
    every workflow ``save_*`` callback runs (plan / brainstorm / compound) so a
    just-written artifact is immediately findable by meaning (the compounding loop).
    It is doc-type-agnostic (``doc_type="file"``, the same row a later
    ``_reindex_files`` would produce), so a single helper serves all three workflows.
    Same embed-then-write sequencing:

      1. GATHER (slow: model inference / remote backend HTTP) the embedding OUTSIDE the write
         lock — calling ``embed_texts`` on the doc body.
      2. WRITE (fast, <10ms) the documents row + vec_documents vector INSIDE one
         ``write_db`` acquisition. The single-writer ``asyncio.Lock`` is NOT reentrant,
         so we never hold it across the embed call (learning f83f5209).

    ``file_path`` is the on-disk artifact path so a later ``_reindex_files`` (which keys
    ``docs/**/*.md`` by absolute path, doc_type ``file``) upserts the SAME row rather
    than a duplicate. ``content`` is the full markdown. Writes go through
    ``write_db`` (the writer pool); the read pool is ``query_only=ON`` (learning
    6130bc49). No-ops if the embedder is unavailable.
    """
    if not is_available():
        return

    h = content_hash(content)
    # GATHER: embedding (remote backend HTTP / local model inference) OUTSIDE the write lock.
    embeddings = await embed_texts([content], input_type="document")
    vec_bytes = serialize_f32(embeddings[0])

    # WRITE: DB operations INSIDE the write lock (fast batch).
    from core.api.db import ensure_vec_documents, write_db

    async with write_db(label="workflows.doc_embed") as db:
        if not await ensure_vec_documents(db):
            return

        cur = await db.execute(
            "SELECT id, content_hash FROM documents WHERE file_path = ?",
            [file_path],
        )
        row = await cur.fetchone()
        if row and row["content_hash"] == h:
            return  # unchanged

        await db.execute(
            """INSERT INTO documents (file_path, project, workspace_id, doc_type, doc_title, content_hash)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(file_path) DO UPDATE SET
                 content_hash = excluded.content_hash, project = excluded.project,
                 workspace_id = excluded.workspace_id, doc_type = excluded.doc_type,
                 doc_title = excluded.doc_title""",
            [file_path, project or "", workspace_id, "file", title, h],
        )
        cur2 = await db.execute(
            "SELECT id FROM documents WHERE file_path = ?",
            [file_path],
        )
        doc_row = await cur2.fetchone()
        doc_id = doc_row["id"]
        await db.execute("DELETE FROM vec_documents WHERE doc_id = ?", [doc_id])
        await db.execute(
            "INSERT INTO vec_documents (doc_id, embedding) VALUES (?, ?)",
            [doc_id, vec_bytes],
        )

        # Track 2 #4 (PROSE only, flag-gated): additionally fan the doc out into
        # the chunks sidecar so retrieval can later max-pool chunk->doc. No-op
        # unless MARVIS_CHUNKING is ON — the whole-doc row above is the unchanged
        # default. Code never reaches here (it is chunked per-symbol upstream).
        if chunking_enabled():
            await persist_prose_chunks(doc_id=str(doc_id), content=content, db=db)


def embedding_is_synchronous() -> bool:
    """True when the active backend is local + fast enough to embed inline on a write.

    Granite local runs in-process (sub-second on CPU) -> embed-on-write can be awaited
    synchronously, so a just-written memory is immediately retrievable by meaning (the
    OSS promise). Rate-limited remote backends (3 RPM on the server free tier)
    must NOT block the write on the embed -> callers fire-and-forget instead. Mirrors the
    backend split in :func:`_embedding_mode`.
    """
    return _embedding_mode() == "granite_local"


async def _ensure_vec_documents(db: aiosqlite.Connection, vec0_path: str) -> None:
    # Prefer the shared cross-platform resolver (sqlite_vec package loader /
    # .so / .dylib probe) so macOS (.dylib) loads; fall back to the passed path.
    from core.api.db import resolve_vec0_loadable

    load_arg, found = resolve_vec0_loadable()
    if not found or load_arg is None:
        load_arg = str(Path(vec0_path))
    await db._execute(db._conn.enable_load_extension, True)
    await db.execute("SELECT load_extension(?)", [load_arg])
    await db.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS vec_documents USING vec0(
            doc_id INTEGER PRIMARY KEY,
            embedding float[512]
        )
    """)


async def _embedding_knn_search(
    db: aiosqlite.Connection,
    vec_bytes: bytes,
    limit: int,
) -> list[_EmbeddingHit]:
    cur = await db.execute(
        """SELECT v.doc_id, v.distance
           FROM vec_documents v
           WHERE v.embedding MATCH ? AND k = ?
           ORDER BY v.distance""",
        [vec_bytes, limit],
    )
    rows = await cur.fetchall()
    return [
        _EmbeddingHit(doc_id=int(row["doc_id"]), distance=float(row["distance"]))
        for row in rows
    ]


async def _bm25_documents_search(
    db: aiosqlite.Connection,
    query: str,
    *,
    limit: int,
) -> list[_Bm25Hit]:
    q_fts = fts5_safe_query(query)
    if not q_fts:
        return []
    try:
        cur = await db.execute(
            """SELECT rowid AS doc_id, bm25(documents_fts) AS score
               FROM documents_fts
               WHERE documents_fts MATCH ?
               ORDER BY score
               LIMIT ?""",
            [q_fts, limit],
        )
        rows = await cur.fetchall()
    except aiosqlite.OperationalError as exc:
        msg = str(exc).lower()
        if (
            "no such table" in msg
            or "syntax" in msg
            or "malformed match" in msg
            or "no such column" in msg
        ):
            return []
        raise
    return [
        _Bm25Hit(doc_id=int(row["doc_id"]), score=float(row["score"] or 0.0))
        for row in rows
    ]


async def _document_columns(db: aiosqlite.Connection) -> set[str]:
    cur = await db.execute("PRAGMA table_info(documents)")
    return {str(row["name"]) for row in await cur.fetchall()}


async def _fetch_document_rows(
    db: aiosqlite.Connection,
    doc_ids: Sequence[int],
    workspace_id: str,
) -> dict[int, dict]:
    if not doc_ids:
        return {}

    columns = await _document_columns(db)
    doc_type_expr = "doc_type" if "doc_type" in columns else "'file' AS doc_type"
    title_expr = "doc_title" if "doc_title" in columns else "file_path AS doc_title"
    salience_expr = "salience" if "salience" in columns else "0.5 AS salience"
    workspace_filter = ""
    params: list[object] = list(doc_ids)
    if "workspace_id" in columns:
        workspace_filter = "AND COALESCE(workspace_id, 'ws_default') = ?"
        params.append(workspace_id)
    archived_filter = ""
    if "archived" in columns:
        archived_filter = "AND COALESCE(archived, 0) = 0"

    placeholders = ",".join("?" * len(doc_ids))
    cur = await db.execute(
        f"""SELECT id, file_path, project, {doc_type_expr}, {title_expr}, {salience_expr}
            FROM documents
            WHERE id IN ({placeholders})
              {workspace_filter}
              {archived_filter}""",
        params,
    )
    rows = await cur.fetchall()
    return {int(row["id"]): dict(row) for row in rows}


def _doc_entity_id(file_path: str) -> str:
    return file_path.split(":", 1)[1] if ":" in file_path else file_path


def _base_result_item(row: Mapping[str, object]) -> dict:
    file_path = str(row["file_path"])
    salience = _normalize_salience(row.get("salience"))
    return {
        "doc_id": _doc_entity_id(file_path),
        "title": row.get("doc_title") or file_path,
        "project": row.get("project") or "",
        "path": file_path,
        "salience": salience,
    }


def _group_ranked_rows(
    ranked_scores: Sequence[_HybridRankScore],
    rows_by_doc_id: Mapping[int, Mapping[str, object]],
    *,
    top_k: int,
) -> dict[str, list[dict]]:
    results = _empty_grouped_results()
    for ranked in ranked_scores:
        row = rows_by_doc_id.get(ranked.doc_id)
        if row is None:
            continue
        doc_type = str(row.get("doc_type") or "file")
        if doc_type not in results:
            continue
        item = _base_result_item(row)
        item["score"] = round(ranked.score, 4)
        item["rrf_score"] = round(ranked.rrf_score, 6)
        item["rank_embedding"] = ranked.rank_embedding
        item["rank_bm25"] = ranked.rank_bm25
        results[doc_type].append(item)

    for doc_type in results:
        results[doc_type].sort(key=lambda item: item["score"], reverse=True)
        results[doc_type] = results[doc_type][:top_k]
    return results


def _group_embedding_only_rows(
    embedding_hits: Sequence[_EmbeddingHit],
    rows_by_doc_id: Mapping[int, Mapping[str, object]],
    *,
    top_k: int,
    salience_weight: float,
    threshold: float,
) -> dict[str, list[dict]]:
    results = _empty_grouped_results()
    for hit in embedding_hits:
        row = rows_by_doc_id.get(hit.doc_id)
        if row is None:
            continue
        doc_type = str(row.get("doc_type") or "file")
        if doc_type not in results:
            continue
        salience = _normalize_salience(row.get("salience"))
        score = _embedding_only_score(
            hit.distance,
            salience,
            salience_weight=salience_weight,
        )
        if score < threshold:
            continue
        item = _base_result_item(row)
        item["score"] = round(score, 4)
        results[doc_type].append(item)

    for doc_type in results:
        results[doc_type].sort(key=lambda item: item["score"], reverse=True)
        results[doc_type] = results[doc_type][:top_k]
    return results


async def search_by_type(
    query: str,
    workspace_id: str,
    db_path: str,
    vec0_path: str,
    top_k: int = 5,
) -> dict[str, list[dict]]:
    """Hybrid RRF search across all doc_types. Returns grouped results.

    Uses separate db + vec_db connections (NOT the request-scoped pooled connections)
    because aiosqlite uses ThreadPoolExecutor(max_workers=1) per connection, so queries
    on the same connection are serialized. Two connections = true parallel I/O.

    Default behavior fuses sqlite-vec semantic KNN with documents_fts BM25
    using Reciprocal Rank Fusion, then applies the salience boost. Set
    SEARCH_BM25_ENABLED=false to use the legacy embedding-only scorer.
    """
    if not is_available():
        raise RuntimeError("Embedding client not initialized")

    embeddings = await embed_texts([query], input_type="query")
    vec_bytes = serialize_f32(embeddings[0])

    # Open dedicated connections — never share request-scoped connections
    db = await aiosqlite.connect(db_path)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode=WAL")
    await db.commit()

    vec_db = await aiosqlite.connect(db_path)
    vec_db.row_factory = aiosqlite.Row
    await vec_db.execute("PRAGMA journal_mode=WAL")
    await vec_db.commit()

    try:
        await _ensure_vec_documents(vec_db, vec0_path)
    except Exception as e:
        await db.close()
        await vec_db.close()
        raise RuntimeError(f"sqlite-vec not available: {e}") from e

    try:
        # Single KNN with large k, then group by doc_type. Previous approach
        # ran the same KNN 4x with k=20, so files/handoffs were drowned out by
        # task-heavy top-20 results.
        fetch_k = max(BM25_FETCH_LIMIT, top_k * 20)
        embedding_hits = await _embedding_knn_search(vec_db, vec_bytes, fetch_k)
        if not embedding_hits:
            return _empty_grouped_results()

        bm25_enabled = _search_bm25_enabled()
        bm25_hits: list[_Bm25Hit] = []
        if bm25_enabled:
            bm25_hits = await _bm25_documents_search(
                db,
                query,
                limit=BM25_FETCH_LIMIT,
            )

        candidate_doc_ids = list(
            dict.fromkeys([hit.doc_id for hit in embedding_hits] + [hit.doc_id for hit in bm25_hits])
        )
        doc_rows = await _fetch_document_rows(db, candidate_doc_ids, workspace_id)
        if not doc_rows:
            return _empty_grouped_results()

        if bm25_enabled:
            salience_by_doc_id = {
                doc_id: _normalize_salience(row["salience"])
                for doc_id, row in doc_rows.items()
            }
            ranked_scores = _fuse_ranked_documents(
                embedding_hits,
                bm25_hits,
                salience_by_doc_id,
                rrf_k=_search_rrf_k(),
                salience_weight=_search_salience_weight(),
                threshold=_search_score_threshold(),
                allowed_doc_ids=set(doc_rows),
            )
            return _group_ranked_rows(ranked_scores, doc_rows, top_k=top_k)

        return _group_embedding_only_rows(
            embedding_hits,
            doc_rows,
            top_k=top_k,
            salience_weight=_search_salience_weight(),
            threshold=_search_score_threshold(),
        )
    finally:
        await db.close()
        await vec_db.close()


async def reindex_project(project: str, db: aiosqlite.Connection) -> dict:
    """Reindex all handoff files for a project. Single connection for atomicity."""
    if not is_available():
        raise RuntimeError("Embedding client not initialized")

    # Find handoff directory
    import core.api.routers.projects as _projects_mod
    project_path = _projects_mod._find_project_path(project)
    if not project_path:
        return {"error": "Project not found", "indexed": 0}

    memory_dir = project_path / "memory"
    if not memory_dir.is_dir():
        return {"error": "No memory directory", "indexed": 0}

    # Cleanup orphans
    await db.execute("DELETE FROM vec_documents WHERE doc_id NOT IN (SELECT id FROM documents)")

    # Load existing hashes
    cursor = await db.execute(
        "SELECT file_path, content_hash FROM documents WHERE project = ?",
        [project],
    )
    existing = {row["file_path"]: row["content_hash"] for row in await cursor.fetchall()}

    # Scan files and find changed ones
    to_embed: list[tuple[str, str]] = []  # (file_path, content)
    for f in sorted(memory_dir.glob("handoff-*.md")):
        if f.is_symlink():
            continue
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        fpath = str(f)
        h = content_hash(text)
        if existing.get(fpath) == h:
            continue
        to_embed.append((fpath, text))

    if not to_embed:
        return {"indexed": 0, "skipped": len(existing), "message": "All up to date"}

    # Batch embed (remote backend max 128 per call)
    indexed = 0
    batch_size = 64
    for i in range(0, len(to_embed), batch_size):
        batch = to_embed[i:i + batch_size]
        texts = [content for _, content in batch]
        embeddings = await embed_texts(texts, input_type="document")

        for (fpath, text), embedding in zip(batch, embeddings):
            h = content_hash(text)
            # Upsert document
            await db.execute(
                "INSERT INTO documents (file_path, project, content_hash) VALUES (?, ?, ?) "
                "ON CONFLICT(file_path) DO UPDATE SET content_hash = excluded.content_hash",
                [fpath, project, h],
            )
            # Get doc_id
            cur = await db.execute("SELECT id FROM documents WHERE file_path = ?", [fpath])
            doc_row = await cur.fetchone()
            doc_id = doc_row["id"]

            # vec0 virtual table does not support ON CONFLICT — DELETE + INSERT
            await db.execute("DELETE FROM vec_documents WHERE doc_id = ?", [doc_id])
            await db.execute(
                "INSERT INTO vec_documents (doc_id, embedding) VALUES (?, ?)",
                [doc_id, serialize_f32(embedding)],
            )
            indexed += 1

        await db.commit()

        # Rate limit: 1s between batches
        if i + batch_size < len(to_embed):
            await asyncio.sleep(1)

    return {"indexed": indexed, "skipped": len(existing) - indexed, "total_files": len(to_embed) + len(existing)}
