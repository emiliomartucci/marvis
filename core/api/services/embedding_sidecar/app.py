"""Shared embedding sidecar — one warm Granite process for the whole fleet.

F1: a standalone FastAPI service bound to loopback :8109. It reuses
``GraniteEmbeddingClient`` DIRECTLY (same pinned revision, same ORT threads, same
token-budget batcher → the RAM-guard comes for free). Two single-worker lanes —
one for queries, one for documents — keep an interactive query from waiting behind
a reindex's document backlog, while peak memory stays bounded (the document lane
serializes the heavy embeds; the query lane's forwards are tiny).

Auth: loopback-only, no token. Every tenant runs as the same ``marvis:marvis``
UID, so a shared secret readable by all of them would be security theater; the
127.0.0.1 bind plus the cgroup ``MemoryMax`` are the real defense, and the worst
case of local non-marvis abuse is a CPU DoS already bounded by both.

Wire format: the response is emitted with the stdlib ``json`` encoder at full
float precision. float32 → Python double → ``repr`` is a byte-exact round-trip,
so the vectors a tenant deserializes are IDENTICAL to the in-process ones —
``orjson``/rounding would silently break the parity the indexes depend on.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from core.api.services.embedding_internal import (
    GraniteEmbeddingClient,
    MODEL_REVISION,
    _int_from_env,
    _physical_cores,
)

logger = logging.getLogger("marvisx.embedding_sidecar")

# Cap per request — the clients chunk, so the worst case of a single query is
# waiting behind one small batch, never a fleet-sized payload.
MAX_TEXTS_PER_REQUEST = 32
DEFAULT_MAX_QUERY_CHARS = 64_000
DEFAULT_MAX_DOC_CHARS = 250_000
DEFAULT_ARENA_RELEASE_DOC_CHARS = 50_000

ClientFactory = Callable[[], GraniteEmbeddingClient]


class EmbedRequest(BaseModel):
    texts: list[str] = Field(..., min_length=1, max_length=MAX_TEXTS_PER_REQUEST)
    input_type: str = Field("document", pattern="^(query|document)$")


def _default_client_factory() -> GraniteEmbeddingClient:
    return GraniteEmbeddingClient(device=os.environ.get("EMBEDDING_DEVICE", "cpu"))


def _effective_ort_threads() -> int:
    # Mirror exactly what embedding_internal._load_onnx_session pins, so /healthz
    # reports the true thread count the fp32 determinism depends on.
    return _int_from_env("EMBEDDING_ORT_THREADS", _physical_cores())


def _release_client(client: GraniteEmbeddingClient) -> None:
    release = getattr(client, "release_model", None)
    if callable(release):
        release()


def _embed_with_disposable_client(
    factory: ClientFactory,
    texts: list[str],
    input_type: str,
) -> list[list[float]]:
    client = factory()
    try:
        return client.embed_texts(texts, input_type=input_type)
    finally:
        _release_client(client)


def create_app(*, client_factory: ClientFactory | None = None) -> FastAPI:
    """Build the sidecar app.

    ``client_factory`` is injectable so the parity test can share ONE model load
    between the in-process reference and the TestClient path.
    """

    factory = client_factory or _default_client_factory
    # F2: doc-lane backpressure ceiling. Default is 1: one active/queued document
    # run, then 503, so a reindex flood cannot pile up ONNX arena allocations.
    max_doc_inflight = max(1, _int_from_env("EMBEDDER_MAX_DOC_INFLIGHT", 1))
    max_query_chars = max(1, _int_from_env("EMBEDDER_MAX_QUERY_CHARS", DEFAULT_MAX_QUERY_CHARS))
    max_doc_chars = max(1, _int_from_env("EMBEDDER_MAX_DOC_CHARS", DEFAULT_MAX_DOC_CHARS))
    arena_release_doc_chars = max(
        0,
        _int_from_env("EMBEDDER_ARENA_RELEASE_DOC_CHARS", DEFAULT_ARENA_RELEASE_DOC_CHARS),
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.start_ts = time.monotonic()
        # Priority lane (F3.1): a dedicated worker per lane. A reindex hammering the
        # document lane never starves an interactive query, because ORT session.run
        # is thread-safe and the tokenizer lock is held only briefly (tokenization),
        # so the two lanes' forward passes run concurrently. Each lane is a single
        # worker, so per-lane peak memory stays bounded (query forwards are tiny).
        app.state.query_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="embed-q")
        app.state.doc_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="embed-d")
        app.state.client = factory()
        app.state.ready = False
        app.state.load_error = None
        # F6: outstanding document embeds (submitted, not yet done). A flood
        # past max_doc_inflight fast-fails so the queue never grows unbounded.
        app.state.doc_inflight = 0

        async def _warm() -> None:
            loop = asyncio.get_running_loop()
            try:
                ok = await loop.run_in_executor(
                    app.state.doc_pool, app.state.client.is_available
                )
            except Exception as exc:  # noqa: BLE001 — never crash-loop on a transient fetch
                app.state.load_error = repr(exc)
                return
            if ok:
                app.state.ready = True
            else:
                app.state.load_error = (
                    app.state.client.load_error_message or "model unavailable"
                )

        # Bind the port immediately; warm the model off the hot path so a transient
        # HF fetch error surfaces on /healthz instead of a systemd restart loop.
        app.state.warm_task = asyncio.create_task(_warm())
        try:
            yield
        finally:
            app.state.warm_task.cancel()
            app.state.query_pool.shutdown(wait=False, cancel_futures=True)
            app.state.doc_pool.shutdown(wait=False, cancel_futures=True)
            _release_client(app.state.client)

    app = FastAPI(title="marvis-embedder", lifespan=lifespan)

    @app.get("/healthz")
    async def healthz() -> JSONResponse:
        client = app.state.client
        if app.state.load_error is not None:
            status = "error"
        elif app.state.ready:
            status = "ready"
        else:
            status = "loading"
        payload = {
            "status": status,
            "model_id": client.active_model_name,
            "revision": MODEL_REVISION,
            "onnx_file": client._onnx_file,
            "ort_threads": _effective_ort_threads(),
            "dim": client.dimensions,
            "max_doc_inflight": max_doc_inflight,
            "max_doc_chars": max_doc_chars,
            "arena_release_doc_chars": arena_release_doc_chars,
            "uptime": round(time.monotonic() - app.state.start_ts, 3),
        }
        if status == "error":
            # Leak-free enum only; the raw load error can carry a path/backend name.
            payload["error"] = "model_load_failed"
        return JSONResponse(payload)

    @app.post("/embed")
    async def embed(req: EmbedRequest) -> JSONResponse:
        char_limit = max_query_chars if req.input_type == "query" else max_doc_chars
        for text in req.texts:
            if len(text) > char_limit:
                raise HTTPException(status_code=413, detail="text too long")
        client = app.state.client
        loop = asyncio.get_running_loop()
        # Route to the priority lane: an interactive query jumps ahead of a reindex's
        # document backlog instead of queuing behind a slow whole-doc embed.
        is_query = req.input_type == "query"
        if not is_query and app.state.doc_inflight >= max_doc_inflight:
            # Doc lane saturated (a reindex/flood): fast-fail so the caller degrades
            # to keyword and the queue never grows unbounded. Queries never hit this.
            raise HTTPException(status_code=503, detail="doc_lane_saturated")
        pool = app.state.query_pool if is_query else app.state.doc_pool
        if not is_query:
            app.state.doc_inflight += 1
        try:
            release_doc_arena = (
                not is_query
                and arena_release_doc_chars > 0
                and any(len(text) >= arena_release_doc_chars for text in req.texts)
            )
            if release_doc_arena:
                embed_call = lambda: _embed_with_disposable_client(
                    factory,
                    list(req.texts),
                    req.input_type,
                )
            else:
                embed_call = lambda: client.embed_texts(
                    list(req.texts),
                    input_type=req.input_type,
                )
            vectors = await loop.run_in_executor(
                pool,
                embed_call,
            )
        except Exception:
            logger.exception("sidecar embed failed")
            raise HTTPException(status_code=503, detail="embedding_unavailable")
        finally:
            if not is_query:
                app.state.doc_inflight -= 1
        # stdlib json full precision (see module docstring); NEVER orjson here.
        return JSONResponse(
            {
                "vectors": vectors,
                "model_id": client.active_model_name,
                "revision": MODEL_REVISION,
                "dim": len(vectors[0]) if vectors else client.dimensions,
            }
        )

    return app


app = create_app()
