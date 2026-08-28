# v2.4.0 - 2026-04-12 - Add learning, inbox_item, audit doc_types to search grouping
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import hashlib
import logging
import os
import struct
import time
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


# --- F2: granite_remote (shared embedding sidecar) ---------------------------
# The sidecar (marvis-embedder.service, loopback) serves native 384-dim Granite
# vectors to the whole fleet. is_available() is sync + hot, so a short TTL cache
# avoids probing /healthz on every call; a model_id/revision mismatch degrades to
# keyword rather than mixing vectors from a different model into the index.
_embedder_http_client = None  # httpx.AsyncClient | None (lazy singleton)
_embedder_health_cache: tuple[float, bool] | None = None  # (checked_at_monotonic, ok)
_EMBEDDER_HEALTH_TTL = 20.0  # seconds
_EMBEDDER_MAX_TEXTS = 32  # mirror the sidecar query per-request cap
# The durable hosted reindex contract intentionally uses a much smaller actual
# document boundary, regardless of the caller's outer collection size.
_EMBEDDER_MAX_DOCUMENT_TEXTS = 1
DEFAULT_EMBEDDER_URL = "http://127.0.0.1:8109"


class EmbeddingBackpressureError(RuntimeError):
    """The document lane is full; retry the job without marking the sidecar down."""


class EmbeddingInputTooLargeError(RuntimeError):
    """One document exceeds the sidecar input cap and must be handled per item."""


def _embedder_url() -> str:
    return os.environ.get("MARVIS_EMBEDDER_URL", DEFAULT_EMBEDDER_URL).rstrip("/")


def _get_embedder_client():
    global _embedder_http_client
    if _embedder_http_client is None:
        import httpx

        _embedder_http_client = httpx.AsyncClient(
            base_url=_embedder_url(),
            timeout=httpx.Timeout(30.0, connect=1.0),
        )
    return _embedder_http_client


def _sidecar_error_detail(response: object) -> str | None:
    try:
        payload = response.json()  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 - error payloads are advisory only
        return None
    if not isinstance(payload, dict):
        return None
    detail = payload.get("detail")
    return detail if isinstance(detail, str) else None


def _probe_embedder_health() -> bool:
    import json as _json
    import urllib.request

    from core.api.services.embedding_internal import DEFAULT_MODEL, MODEL_REVISION

    try:
        with urllib.request.urlopen(f"{_embedder_url()}/healthz", timeout=2.0) as resp:
            if resp.status != 200:
                return False
            body = _json.loads(resp.read().decode("utf-8"))
    except Exception:  # noqa: BLE001 - any probe failure = not available
        return False
    if body.get("status") != "ready":
        return False
    if body.get("model_id") != DEFAULT_MODEL or body.get("revision") != MODEL_REVISION:
        logger.error(
            "Embedder sidecar model/revision mismatch: got model=%r revision=%r "
            "expected model=%r revision=%r - refusing to embed (keyword-only)",
            body.get("model_id"),
            body.get("revision"),
            DEFAULT_MODEL,
            MODEL_REVISION,
        )
        return False
    return True


def _embedder_health_ok() -> bool:
    global _embedder_health_cache
    now = time.monotonic()
    if _embedder_health_cache is not None:
        checked_at, ok = _embedder_health_cache
        if now - checked_at < _EMBEDDER_HEALTH_TTL:
            return ok
    ok = _probe_embedder_health()
    _embedder_health_cache = (now, ok)
    return ok


async def _embed_granite_remote_texts(
    texts: list[str],
    input_type: str,
) -> list[list[float]]:
    """Embed via the shared sidecar while preserving its health contract.

    A saturated document lane and a too-large document are actionable request
    outcomes, not evidence that the shared sidecar is unavailable. Only the
    remaining transport and HTTP failures poison the short readiness cache.
    """
    if not texts:
        return []
    global _embedder_health_cache
    client = _get_embedder_client()
    raw: list[list[float]] = []
    request_batch_size = (
        _EMBEDDER_MAX_TEXTS
        if input_type == "query"
        else _EMBEDDER_MAX_DOCUMENT_TEXTS
    )
    try:
        for start in range(0, len(texts), request_batch_size):
            chunk = texts[start : start + request_batch_size]
            resp = await client.post(
                "/embed", json={"texts": chunk, "input_type": input_type}
            )
            if (
                input_type == "document"
                and resp.status_code == 503
                and _sidecar_error_detail(resp) == "doc_lane_saturated"
            ):
                raise EmbeddingBackpressureError("Embedding document lane is saturated")
            if input_type == "document" and resp.status_code == 413:
                raise EmbeddingInputTooLargeError("Embedding document exceeds sidecar limit")
            resp.raise_for_status()
            raw.extend(resp.json()["vectors"])
    except (EmbeddingBackpressureError, EmbeddingInputTooLargeError):
        raise
    except Exception as exc:  # noqa: BLE001 - unknown failure -> fail-soft keyword
        _embedder_health_cache = (time.monotonic(), False)
        logger.warning("Embedder sidecar request failed: %s", exc)
        raise RuntimeError("Embedding sidecar unavailable") from exc
    return [_coerce_dimensions_for_vec_documents(v) for v in raw]


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
        _get_embedder_client()  # lazy httpx client, no model warm
        logger.info(
            "Granite remote embedding mode: sidecar client initialized (url=%s)",
            _embedder_url(),
        )


def is_available() -> bool:
    mode = _embedding_mode()
    if mode in {"remote", "dual"}:
        backend = _remote_backend()
        if backend is not None and backend.client_ready():
            return True
        if mode == "remote" and _remote_auto_fallback_enabled():
            return _get_granite_client().is_available()
        return False
    if mode == "granite_local":
        # F1: honest readiness. The client's is_available() actually attempts the
        # load and records any error, unlike can_attempt_load which stays True
        # until a crash is recorded → it reported "available" before the model had
        # ever loaded, letting a dead retriever degrade to keyword-only silently.
        return _get_granite_client().is_available()
    if mode == "granite_remote":
        return _embedder_health_ok()
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
        if _remote_auto_fallback_enabled():
            try:
                backend = _remote_backend()
                if backend is not None and backend.client_ready():
                    return await _embed_remote_texts(texts, input_type=input_type)
                logger.warning("Remote embedding backend unavailable; falling back to Granite local")
            except Exception:
                logger.exception("Remote embedding backend failed; falling back to Granite local")
            return await _embed_granite_texts(
                texts,
                input_type=input_type,
                batch_size=batch_size,
            )
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
    if mode == "granite_remote":
        return await _embed_granite_remote_texts(texts, input_type=input_type)
    raise NotImplementedError(f"Unhandled embedding mode: {mode!r}")


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


def _remote_auto_fallback_enabled() -> bool:
    """Allow auto-resolved remote deployments to degrade to local Granite.

    An explicit ``EMBEDDING_MODE=remote`` remains strict. With no explicit mode,
    deploys that carry a remote backend use it when healthy, but search/reindex
    can still work through the local OSS engine when the remote client is down.
    """
    raw_mode = os.environ.get("EMBEDDING_MODE")
    if raw_mode and raw_mode.strip():
        return False
    raw = os.environ.get("EMBEDDING_REMOTE_FALLBACK_LOCAL", "true").strip().lower()
    return raw not in {"0", "false", "no", "off"}


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


# --- Memory-freshness v2a Phase 1: FTS body refresh (MARVIS_FTS_BODIES) -----

# Hard cap on the FTS body size — mirrors _is_loadable_document_path's 500KB
# file gate so a pathological inbox body can't balloon the FTS index row.
_FTS_BODY_MAX_CHARS = 500_000


def fts_bodies_enabled() -> bool:
    """True only when MARVIS_FTS_BODIES is ON (settings-backed, default False)."""
    from core.api.config import settings

    return bool(settings.fts_bodies_enabled)


async def refresh_documents_fts_row(
    db: aiosqlite.Connection,
    *,
    doc_id: int | str,
    title: str,
    content: str | None,
    force: bool = False,
) -> None:
    """B-fix: overwrite the trigger-degraded documents_fts row with the real body.

    The migration-136 INSERT/UPDATE triggers write ``file_path`` into the FTS
    ``content`` column, so every doc written after the one-time migration
    backfill has no body in the lexical lane. Callers invoke this right after
    the ``documents`` upsert, INSIDE the same transaction, passing the title
    and body text they already have in scope — no extra I/O is performed here,
    so the single-writer lock is never held across a read (learning f83f5209).

    Contract:
    * Flag OFF (default) → immediate no-op, byte-identical behavior.
    * ``force=True`` bypasses the flag for write paths that already hold the
      body and must make a just-saved artifact lexically recoverable.
    * Flag ON → DELETE+INSERT by rowid (the idempotent shape the migration
      backfill hook uses in ``db._backfill_documents_fts``).
    * Fail-open: a missing documents_fts table (pre-migration DB) degrades to
      a logged no-op, never an error — search must keep working.

    Known asymmetry (accepted in the plan): task callers only have
    title+status+project in scope, so a runtime refresh writes a thinner body
    than the migration resolver (which joins description+tags). Still strictly
    better than the trigger's file_path-only row.
    """
    # RBAC F4 universal guard: an owner-confidential doc must NEVER carry a
    # lexical row, whichever embed lane calls us. If documents.confidential=1,
    # purge any residual fts row (by the stable doc_id column) and never
    # re-insert. This is the single chokepoint every reindex lane funnels
    # through, so no lane can re-expose a purged confidential file.
    try:
        cur = await db.execute("SELECT confidential FROM documents WHERE id = ?", [doc_id])
        row = await cur.fetchone()
        if row is not None and bool(row[0]):
            await db.execute("DELETE FROM documents_fts WHERE doc_id = ?", [doc_id])
            return
    except aiosqlite.OperationalError:
        pass  # pre-162 DB has no confidential column → normal behavior
    if not force and not fts_bodies_enabled():
        return
    body = (content or title or "")[:_FTS_BODY_MAX_CHARS]
    try:
        await db.execute("DELETE FROM documents_fts WHERE doc_id = ?", [doc_id])
        await db.execute(
            "INSERT INTO documents_fts(rowid, doc_id, title, content) VALUES (?, ?, ?, ?)",
            [doc_id, doc_id, title or "", body],
        )
    except aiosqlite.OperationalError as exc:  # pre-migration DB → fail open
        logger.warning("refresh_documents_fts_row degraded (doc_id=%s): %s", doc_id, exc)


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
    vec_db: aiosqlite.Connection | None = None,
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

    # A-span (Phase 2): mirror every chunk vector into the vec_chunks vec0
    # sidecar (keyed on chunks.rowid) so the read path can KNN. Fail-open: a
    # connection without the vec0 extension keeps the BLOB-only behavior.
    vec_conn = vec_db if vec_db is not None else db
    vec_ok = await ensure_vec_chunks(vec_conn)

    async def _drop_vec_rows(chunk_ids: Sequence[str]) -> None:
        if not vec_ok or not chunk_ids:
            return
        ph = ",".join("?" * len(chunk_ids))
        cur = await db.execute(
            f"SELECT rowid FROM chunks WHERE chunk_id IN ({ph})", list(chunk_ids)
        )
        rowids = [int(r["rowid"]) for r in await cur.fetchall()]
        for rid in rowids:
            await vec_conn.execute(
                "DELETE FROM vec_chunks WHERE chunk_rowid = ?", [rid]
            )

    tokenizer = _get_granite_client().tokenizer_only()
    chunks = chunk_prose(content, tokenizer)
    if not chunks:
        cur = await db.execute(
            "SELECT chunk_id FROM chunks WHERE doc_id = ?", [doc_id]
        )
        await _drop_vec_rows([r["chunk_id"] for r in await cur.fetchall()])
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
        await _drop_vec_rows(stale_ids)
        placeholders = ",".join("?" * len(stale_ids))
        await db.execute(
            f"DELETE FROM chunks WHERE chunk_id IN ({placeholders})", stale_ids
        )

    if not pending:
        return 0

    raw = content.encode("utf-8")
    texts = [raw[c.span_start : c.span_end].decode("utf-8", "replace") for _, _, c in pending]
    try:
        embedded = list(zip(pending, await embed_texts(texts, input_type="document")))
    except EmbeddingInputTooLargeError:
        # One oversized chunk must not kill the document — nor, upstream, the
        # whole reindex job (2026-08-05: one such input killed every full
        # reindex at the very end, with no name in the log, leaving 32
        # documents unembedded). Retry chunk-by-chunk, NAME the culprit, keep
        # the rest.
        embedded = []
        for entry, text in zip(pending, texts):
            try:
                vec = (await embed_texts([text], input_type="document"))[0]
            except EmbeddingInputTooLargeError:
                _, idx, c = entry
                logger.warning(
                    "chunk embedding exceeds sidecar limit, skipping: "
                    "doc_id=%s chunk_idx=%s bytes=%s",
                    doc_id,
                    idx,
                    c.span_end - c.span_start,
                )
                continue
            embedded.append((entry, vec))
    for (chunk_id, idx, c), vec in embedded:
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
        if vec_ok:
            cur = await db.execute(
                "SELECT rowid FROM chunks WHERE chunk_id = ?", [chunk_id]
            )
            row = await cur.fetchone()
            if row is not None:
                rid = int(row["rowid"])
                await vec_conn.execute(
                    "DELETE FROM vec_chunks WHERE chunk_rowid = ?", [rid]
                )
                await vec_conn.execute(
                    "INSERT INTO vec_chunks (chunk_rowid, embedding) VALUES (?, ?)",
                    [rid, serialize_f32(list(vec))],
                )
    return len(embedded)


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


# --- Memory-freshness v2a Phase 2: A-span read path (MARVIS_SEARCH_SPANS) ----

# Window half-size around the winning chunk, in LINES, and the hard cap on the
# returned evidence text. Fixed for now — tune only if the benchmark judge says
# spans truncate the "why" (plan, Phase 2 open question).
_SPAN_WINDOW_LINES = 12
_SPAN_MAX_CHARS = 2_000

# Chunk-KNN over-fetch so a doc is not lost when its best chunk ranks deep
# (aggregate max-pool fan-in, spec-flow: the doc-level fetch_n is too small).
_CHUNK_FETCH_MULTIPLIER = 5
_CHUNK_FETCH_CAP = 1_000


def search_spans_enabled() -> bool:
    """True only when MARVIS_SEARCH_SPANS is ON (settings-backed, default False)."""
    from core.api.config import settings

    return bool(settings.search_spans_enabled)


def expand_span_to_window(
    raw: bytes,
    span_start: int,
    span_end: int,
    *,
    window_lines: int = _SPAN_WINDOW_LINES,
    max_chars: int = _SPAN_MAX_CHARS,
) -> tuple[str, int, int]:
    """Expand a UTF-8 byte span to line boundaries ± ``window_lines`` lines.

    Pure bytes-domain walk (no full-text decode, so offsets stay exact even
    with multibyte content): clamp the span, walk back ``window_lines``+1
    newlines for the window start, forward the same for the end, then decode
    just the window slice (``errors="replace"``) and cap at ``max_chars``.

    Returns ``(text, line_start, line_end)`` with 1-based line numbers into
    the original document.
    """
    n = len(raw)
    start = min(max(span_start, 0), n)
    end = min(max(span_end, start), n)

    # Walk BACK: the window starts after the (window_lines+1)-th newline
    # before the span (or at byte 0).
    win_start = start
    seen = 0
    while win_start > 0:
        nl = raw.rfind(b"\n", 0, win_start)
        if nl == -1:
            win_start = 0
            break
        seen += 1
        if seen > window_lines:
            win_start = nl + 1
            break
        win_start = nl
    else:
        win_start = 0
    if win_start < 0:
        win_start = 0

    # Walk FORWARD: the window ends at the (window_lines+1)-th newline after
    # the span (or EOF).
    win_end = end
    seen = 0
    while win_end < n:
        nl = raw.find(b"\n", win_end)
        if nl == -1:
            win_end = n
            break
        seen += 1
        if seen > window_lines:
            win_end = nl
            break
        win_end = nl + 1
    else:
        win_end = n

    slice_start = win_start
    slice_end = win_end
    if slice_end - slice_start > max_chars:
        half = max_chars // 2
        slice_start = max(win_start, start - half)
        slice_end = min(win_end, slice_start + max_chars)
        slice_start = max(win_start, slice_end - max_chars)

    text = raw[slice_start:slice_end].decode("utf-8", "replace")
    line_start = raw.count(b"\n", 0, slice_start) + 1
    line_end = raw.count(b"\n", 0, slice_end) + 1
    return text, line_start, line_end


async def ensure_vec_chunks(db: aiosqlite.Connection) -> bool:
    """Create the vec_chunks vec0 sidecar (extension must already be loaded).

    Mirrors the runtime-ensured ``vec_documents`` pattern: keyed on the
    ``chunks`` table's implicit rowid, 512-dim padded layout (``serialize_f32``).
    Returns False (fail-open) when vec0 is not loadable on this connection.
    """
    try:
        await db.execute(
            """CREATE VIRTUAL TABLE IF NOT EXISTS vec_chunks USING vec0(
                chunk_rowid INTEGER PRIMARY KEY,
                embedding float[512]
            )"""
        )
        return True
    except aiosqlite.OperationalError as exc:
        logger.warning("ensure_vec_chunks degraded: %s", exc)
        return False


async def _chunk_knn_search(
    conn: aiosqlite.Connection,
    vec_bytes: bytes,
    *,
    limit: int,
) -> list[dict]:
    """KNN over vec_chunks, joined back to the chunks sidecar metadata.

    Returns rows shaped for ``aggregate_chunk_hits_to_docs``: ``doc_id``,
    ``chunk_id``, ``score`` (cosine-ish, higher better — same distance→
    similarity mapping the doc lane uses) plus the span offsets the #2
    citation layer needs. Fail-open: missing tables → empty list.
    """
    try:
        cur = await conn.execute(
            """SELECT v.chunk_rowid, v.distance
               FROM vec_chunks v
               WHERE v.embedding MATCH ? AND k = ?
               ORDER BY v.distance""",
            [vec_bytes, limit],
        )
        knn_rows = await cur.fetchall()
    except aiosqlite.OperationalError as exc:
        logger.warning("chunk knn degraded: %s", exc)
        return []
    if not knn_rows:
        return []

    by_rowid = {int(r["chunk_rowid"]): float(r["distance"]) for r in knn_rows}
    placeholders = ",".join("?" * len(by_rowid))
    try:
        cur = await conn.execute(
            f"""SELECT rowid, chunk_id, doc_id, span_start, span_end
                FROM chunks WHERE rowid IN ({placeholders})""",
            list(by_rowid),
        )
        meta_rows = await cur.fetchall()
    except aiosqlite.OperationalError as exc:
        logger.warning("chunk knn meta degraded: %s", exc)
        return []

    out: list[dict] = []
    for r in meta_rows:
        distance = by_rowid[int(r["rowid"])]
        similarity = min(max(1.0 - (distance / 2.0), 0.0), 1.0)
        out.append(
            {
                "doc_id": r["doc_id"],
                "chunk_id": r["chunk_id"],
                "score": similarity,
                "distance": distance,
                "span_start": int(r["span_start"] or 0),
                "span_end": int(r["span_end"] or 0),
            }
        )
    out.sort(key=lambda h: (-h["score"], str(h["chunk_id"])))
    return out


async def _attach_spans(
    grouped: dict[str, list[dict]],
    anchors_by_path: dict[str, dict],
) -> None:
    """Attach span_text/span_path/span_line_* to grouped hits, IN PLACE.

    ``anchors_by_path`` maps a file-backed document path → winning chunk span.
    File reads happen via ``asyncio.to_thread`` (never sync in the async search
    path) and every failure (file moved/deleted, decode error) degrades to a
    hit WITHOUT span fields — search never breaks on a missing file.
    """

    def _read(path: str) -> bytes | None:
        try:
            p = Path(path)
            if not p.is_file() or p.is_symlink() or p.stat().st_size > 500_000:
                return None
            return p.read_bytes()
        except OSError:
            return None

    for bucket in grouped.values():
        for item in bucket:
            path = item.get("path")
            anchor = anchors_by_path.get(str(path)) if path else None
            if anchor is None:
                continue
            raw = await asyncio.to_thread(_read, str(path))
            if raw is None:
                continue
            text, line_start, line_end = expand_span_to_window(
                raw, anchor["span_start"], anchor["span_end"]
            )
            if not text.strip():
                continue
            item["span_text"] = text
            item["span_path"] = str(path)
            item["span_line_start"] = line_start
            item["span_line_end"] = line_end


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

    # RBAC F4: never re-embed an owner-confidential doc. Purge any residual
    # vec/fts rows and leave the documents row as the confidential tombstone.
    try:
        cols = {str(c[1]) for c in await (await db.execute("PRAGMA table_info(documents)")).fetchall()}
    except Exception:  # noqa: BLE001
        cols = set()
    if row and "confidential" in cols:
        cur = await db.execute("SELECT confidential FROM documents WHERE id = ?", [row["id"]])
        conf = await cur.fetchone()
        if conf is not None and bool(conf[0]):
            try:
                await vec_db.execute("DELETE FROM vec_documents WHERE doc_id = ?", [row["id"]])
            except Exception as exc:  # noqa: BLE001
                logger.warning("vec purge (confidential upsert skip) failed doc=%s: %s", row["id"], exc)
            await db.execute("DELETE FROM documents_fts WHERE doc_id = ?", [row["id"]])
            return False

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
    # B-fix (MARVIS_FTS_BODIES): repair the trigger-degraded FTS row in the
    # same transaction as the documents upsert. No-op when the flag is off.
    await refresh_documents_fts_row(db, doc_id=doc_id, title=doc_title, content=content)
    # A-span (Phase 2, gap S2): file-backed prose docs fan out into the chunks
    # sidecar on EVERY write path, not just the workflow-doc one. No-op unless
    # MARVIS_CHUNKING is ON.
    if chunking_enabled() and file_path.startswith("/"):
        await persist_prose_chunks(
            doc_id=str(doc_id), content=content, db=db, vec_db=vec_db
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
    if not await asyncio.to_thread(is_available):
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
        # B-fix (MARVIS_FTS_BODIES): repair the trigger-degraded FTS row.
        await refresh_documents_fts_row(db, doc_id=doc_id, title=title, content=content)


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
    if not await asyncio.to_thread(is_available):
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
        # B-fix (MARVIS_FTS_BODIES): repair the trigger-degraded FTS row.
        await refresh_documents_fts_row(db, doc_id=doc_id, title=title, content=content)


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
        # B-fix (MARVIS_FTS_BODIES): repair the trigger-degraded FTS row.
        await refresh_documents_fts_row(db, doc_id=doc_id, title=name, content=content)


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
        # B-fix (MARVIS_FTS_BODIES): repair the trigger-degraded FTS row. The
        # FULL body goes to the lexical index (the embed used a 500-char
        # snippet for vector quality; BM25 wants the whole text).
        await refresh_documents_fts_row(
            db,
            doc_id=doc_id,
            title=title or "",
            content="\n".join(filter(None, [title, content or ""])),
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
        # B-fix (MARVIS_FTS_BODIES): repair the trigger-degraded FTS row.
        await refresh_documents_fts_row(db, doc_id=doc_id, title=title, content=content)

        # Track 2 #4 (PROSE only, flag-gated): additionally fan the doc out into
        # the chunks sidecar so retrieval can later max-pool chunk->doc. No-op
        # unless MARVIS_CHUNKING is ON — the whole-doc row above is the unchanged
        # default. Code never reaches here (it is chunked per-symbol upstream).
        if chunking_enabled():
            await persist_prose_chunks(doc_id=str(doc_id), content=content, db=db)


async def upsert_doc_document_fts_row(
    db: aiosqlite.Connection,
    *,
    file_path: str,
    title: str,
    content: str,
    project: str,
    workspace_id: str,
    force_body: bool = False,
) -> int:
    """Upsert a saved workflow doc into ``documents`` and repair its FTS body.

    This is the lexical half of :func:`embed_doc_document`. Workflow ``save_*``
    callbacks call it before trying vector embedding so a saved plan/brainstorm/
    solution is immediately reachable through BM25 even when the embedding
    backend is unavailable. It intentionally does not touch ``vec_documents``.
    """
    h = content_hash(content)
    await db.execute(
        """INSERT INTO documents (file_path, project, workspace_id, doc_type, doc_title, content_hash)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(file_path) DO UPDATE SET
             content_hash = excluded.content_hash, project = excluded.project,
             workspace_id = excluded.workspace_id, doc_type = excluded.doc_type,
             doc_title = excluded.doc_title""",
        [file_path, project or "", workspace_id, "file", title, h],
    )
    cur = await db.execute("SELECT id FROM documents WHERE file_path = ?", [file_path])
    row = await cur.fetchone()
    doc_id = int(row["id"])
    await refresh_documents_fts_row(
        db,
        doc_id=doc_id,
        title=title,
        content=content,
        force=force_body,
    )
    return doc_id


async def index_doc_document_fts(
    *,
    file_path: str,
    title: str,
    content: str,
    project: str,
    workspace_id: str,
) -> int | None:
    """Lexically index a saved workflow doc without requiring embeddings."""
    from core.api.db import write_db

    async with write_db(label="workflows.doc_fts") as db:
        return await upsert_doc_document_fts_row(
            db,
            file_path=file_path,
            title=title,
            content=content,
            project=project,
            workspace_id=workspace_id,
            force_body=True,
        )


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
    workspace_id: str | None = None,
) -> list[_Bm25Hit]:
    q_fts = fts5_safe_query(query)
    if not q_fts:
        return []
    try:
        if workspace_id is None:
            cur = await db.execute(
                """SELECT rowid AS doc_id, bm25(documents_fts) AS score
                   FROM documents_fts
                   WHERE documents_fts MATCH ?
                   ORDER BY score
                   LIMIT ?""",
                [q_fts, limit],
            )
        else:
            columns = await _document_columns(db)
            if "workspace_id" not in columns:
                return []
            workspace_clause = (
                "AND d.workspace_id = ?"
                if "workspace_id" in columns
                else ""
            )
            archived_clause = (
                "AND COALESCE(d.archived, 0) = 0"
                if "archived" in columns
                else ""
            )
            confidential_clause = (
                "AND COALESCE(d.confidential, 0) = 0"
                if "confidential" in columns
                else ""
            )
            params: list[object] = [q_fts]
            if workspace_clause:
                params.append(workspace_id)
            params.append(limit)
            cur = await db.execute(
                f"""SELECT documents_fts.rowid AS doc_id,
                          bm25(documents_fts) AS score
                   FROM documents_fts
                   JOIN documents d ON d.id = documents_fts.rowid
                   WHERE documents_fts MATCH ?
                     {workspace_clause}
                     {archived_clause}
                     {confidential_clause}
                   ORDER BY score
                   LIMIT ?""",
                params,
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
        workspace_filter = "AND workspace_id = ?"
        params.append(workspace_id)
    archived_filter = ""
    if "archived" in columns:
        archived_filter = "AND COALESCE(archived, 0) = 0"
    # RBAC F4: mirror of the archived filter — a purged owner-confidential doc
    # must never be retrieved-then-dropped, not even via stale vec/fts rows.
    confidential_filter = ""
    if "confidential" in columns:
        confidential_filter = "AND COALESCE(confidential, 0) = 0"

    placeholders = ",".join("?" * len(doc_ids))
    cur = await db.execute(
        f"""SELECT id, file_path, project, {doc_type_expr}, {title_expr}, {salience_expr}
            FROM documents
            WHERE id IN ({placeholders})
              {workspace_filter}
              {archived_filter}
              {confidential_filter}""",
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

        # A-span (Phase 2, flag-gated): max-pool chunk-KNN into the semantic
        # ranking. A doc's distance becomes min(doc-level, best-chunk-level) —
        # precise paragraphs beat diluted whole-doc vectors — and the winning
        # chunk is kept as the span anchor for the response. Flag OFF → this
        # block never runs and the ranking is byte-identical to today.
        span_anchor_by_doc_id: dict[int, dict] = {}
        if search_spans_enabled() and chunking_enabled():
            chunk_fetch_k = min(fetch_k * _CHUNK_FETCH_MULTIPLIER, _CHUNK_FETCH_CAP)
            chunk_hits = await _chunk_knn_search(
                vec_db, vec_bytes, limit=chunk_fetch_k
            )
            if chunk_hits:
                span_by_chunk_id = {h["chunk_id"]: h for h in chunk_hits}
                pooled = aggregate_chunk_hits_to_docs(chunk_hits)
                merged: dict[int, float] = {
                    h.doc_id: h.distance for h in embedding_hits
                }
                for pd in pooled:
                    try:
                        did = int(str(pd["doc_id"]))
                    except ValueError:
                        continue  # non-integer doc key → not a documents row
                    anchor = span_by_chunk_id.get(pd["chunk_id"])
                    if anchor is None:
                        continue
                    span_anchor_by_doc_id[did] = anchor
                    chunk_distance = float(anchor["distance"])
                    prev = merged.get(did)
                    if prev is None or chunk_distance < prev:
                        merged[did] = chunk_distance
                embedding_hits = sorted(
                    (
                        _EmbeddingHit(doc_id=did, distance=dist)
                        for did, dist in merged.items()
                    ),
                    key=lambda h: (h.distance, h.doc_id),
                )[:fetch_k]

        bm25_enabled = _search_bm25_enabled()
        bm25_hits: list[_Bm25Hit] = []
        if bm25_enabled:
            bm25_hits = await _bm25_documents_search(
                db,
                query,
                limit=BM25_FETCH_LIMIT,
                workspace_id=workspace_id,
            )

        candidate_doc_ids = list(
            dict.fromkeys([hit.doc_id for hit in embedding_hits] + [hit.doc_id for hit in bm25_hits])
        )
        doc_rows = await _fetch_document_rows(db, candidate_doc_ids, workspace_id)
        if not doc_rows:
            return _empty_grouped_results()

        # A-span: resolve anchors to file-backed paths (row-backed docs have no
        # on-disk body to window into; their span stays None by design).
        anchors_by_path: dict[str, dict] = {}
        for did, anchor in span_anchor_by_doc_id.items():
            row = doc_rows.get(did)
            if row is None:
                continue
            path = str(row.get("file_path") or "")
            if path.startswith("/"):
                anchors_by_path[path] = anchor

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
            results = _group_ranked_rows(ranked_scores, doc_rows, top_k=top_k)
            if anchors_by_path:
                await _attach_spans(results, anchors_by_path)
            return results

        results = _group_embedding_only_rows(
            embedding_hits,
            doc_rows,
            top_k=top_k,
            salience_weight=_search_salience_weight(),
            threshold=_search_score_threshold(),
        )
        if anchors_by_path:
            await _attach_spans(results, anchors_by_path)
        return results
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
            # B-fix (MARVIS_FTS_BODIES): no doc_title in this legacy path — the
            # file_path-as-title mirrors the migration backfill fallback.
            await refresh_documents_fts_row(db, doc_id=doc_id, title=fpath, content=text)
            # A-span (Phase 2, gap S2): keep the chunks sidecar in sync on the
            # reindex path too. No-op unless MARVIS_CHUNKING is ON.
            if chunking_enabled() and fpath.startswith("/"):
                await persist_prose_chunks(
                    doc_id=str(doc_id), content=text, db=db
                )
            indexed += 1

        await db.commit()

        # Rate limit: 1s between batches
        if i + batch_size < len(to_embed):
            await asyncio.sleep(1)

    return {"indexed": indexed, "skipped": len(existing) - indexed, "total_files": len(to_embed) + len(existing)}
