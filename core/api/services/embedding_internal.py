"""Self-hosted embedding client for Granite 97M multilingual models.

Torch-free engine: runs ``ibm-granite/granite-embedding-97m-multilingual-r2``
(384-dim, ModernBert) directly via ``onnxruntime`` + the Rust ``tokenizers``
package + ``huggingface_hub``. No ``sentence-transformers`` / ``optimum`` /
``torch`` — those drag in ~1GB of wheels even when inference runs on ONNX.

The public shape mirrors the previous client so ``embedding_service.py`` and the
MCP/search layers are untouched (no-fork): ``embed_texts(texts, input_type,
batch_size) -> list[list[float]]`` plus ``is_available()`` / ``can_attempt_load``
/ ``active_model_name`` / ``dimensions`` / ``cache_folder``. Model loading is
lazy: constructing the client only does inexpensive env + RAM checks; the ONNX
session and tokenizer are created on the first embed (or warmed in
``is_available()``).

Pooling is **CLS** (``last_hidden_state[:, 0]``) then L2-normalize — NOT mean.
This matches the model's ``1_Pooling/config.json`` (``pooling_mode_cls_token=true``).
"""

from __future__ import annotations

import importlib
import logging
import math
import os
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger("marvisx.embedding_internal")

DEFAULT_MODEL = "ibm-granite/granite-embedding-97m-multilingual-r2"
DEFAULT_RAM_THRESHOLD_MB = 4096
EXPECTED_DIMENSIONS = 384
_MB = 1024 * 1024

# --- Pinned model revision (review delta #2) --------------------------------
# Pin the exact commit so the model graph + tokenizer never drift silently under
# us (a future re-export would break the cosine>=0.999 invariant the search
# pipeline depends on). The equivalence fixture is generated from THIS revision.
# https://huggingface.co/ibm-granite/granite-embedding-97m-multilingual-r2
MODEL_REVISION = "835ad14087e140460703cf0fae09f97d469d65c2"

# --- ONNX graph selection ---------------------------------------------------
# The HF repo ships two ONNX graphs: onnx/model.onnx (fp32, ~390MB) and
# onnx/model_quint8_avx2.onnx (int8, ~98MB). There is no fp16 export in the repo.
# Precision was measured empirically via tests/test_embedding_onnx_equivalence.py
# (review delta #7): fp32 holds cosine>=0.999 vs sentence-transformers on the
# IT+EN code+prose corpus; int8 drops below it (1-3 recall points at risk), so
# fp32 is the default. Override with EMBEDDING_ONNX_FILE for benchmarking only.
_ONNX_FILE_DEFAULT = "onnx/model.onnx"

# --- Tokenizer constants (review delta #3) ----------------------------------
# Hardcoded with a source comment rather than parsing config at runtime (a
# parser + fallback is the worst of both worlds for a single fixed model). The
# equivalence test catches any drift from these values.
# Source: config.json (pad_token_id=179935, cls_token_id=179934),
#         tokenizer_config.json (pad_token="<|endoftext|>", model_max_length=32768),
#         special_tokens_map.json (pad_token "<|endoftext|>").
PAD_ID = 179935
PAD_TOKEN = "<|endoftext|>"
CLS_TOKEN_ID = 179934  # added by the tokenizer template, never injected by hand

# Truncation length: MUST match what sentence-transformers used, or long inputs
# get a different CLS vector => cosine<0.999 (verified: truncating at 512 dropped
# two >512-token corpus strings to cos~0.994). sentence-transformers reads this
# model's truncation from sentence_bert_config.json => max_seq_length=32768 (NOT
# the 512 the plan assumed; this ModernBert R2 model has a 32768 context window,
# matching config.json max_position_embeddings). Hardcode 32768 to mirror ST.
# Source: sentence_bert_config.json (max_seq_length=32768), config.json
# (max_position_embeddings=32768). Padding is to the per-batch max, not MAX_LEN,
# so short inputs stay cheap; only genuinely long docs pay the longer sequence.
MAX_LEN = 32768

# F2 (OOM safety): bound the reindex memory. Granite attention is quadratic in
# sequence length, so a single 32k-token doc co-batched at the full context drove
# the ~80GB peak. Two env-overridable knobs (via _int_from_env):
#   * EMBEDDING_MAX_TOKENS — per-doc hard cap (truncate-with-warning above it).
#   * EMBEDDING_TOKEN_BUDGET — batch budget (len(batch) × max_seqlen): THIS is what
#     guarantees the bound; the per-doc cap alone does NOT (a batch of all-long
#     docs still co-batches). Default: a doc at the 4096 cap fits 4 per batch
#     (4×4096=16384); shorter docs pack more, up to the count cap. The equivalence
#     corpus is < cap, so cosine stays unchanged.
_DEFAULT_MAX_TOKENS = 4096
_DEFAULT_TOKEN_BUDGET = 16384

# Only fetch the ONNX graph + tokenizer/config — never *.safetensors / *.bin
# (those are the torch weights we are eliminating).
_ALLOW_PATTERNS = [
    "onnx/model.onnx",
    "onnx/model.onnx_data",  # external-data sidecar if the graph is split (safe no-op if absent)
    "onnx/model_quint8_avx2.onnx",
    "tokenizer.json",
    "tokenizer_config.json",
    "special_tokens_map.json",
    "config.json",
    "1_Pooling/config.json",
    "config_sentence_transformers.json",
]


class GraniteEmbeddingClient:
    """Lazy torch-free onnxruntime client for local Granite embeddings.

    Granite is the only local model. Machines below the RAM floor are NOT
    silently downgraded to a weaker model: the client emits an actionable
    warning pointing at the hardware requirement and proceeds with Granite.

    Thread-safety (review delta #4): the API is async and offloads embeds via
    ``run_in_executor``, so multiple threads may call ``embed_texts`` / trigger
    the lazy load concurrently. The lazy load is guarded by ``_load_lock`` (two
    threads must not download/instantiate twice). ``InferenceSession.run`` is
    thread-safe, but a ``tokenizers.Tokenizer`` configured with
    ``enable_padding``/``enable_truncation`` mutates internal state and is NOT
    thread-safe, so each embed call tokenizes under ``_encode_lock``.
    """

    def __init__(
        self,
        model_name: str = DEFAULT_MODEL,
        device: str = "cpu",
    ) -> None:
        self.model_name = model_name
        self.device = device
        self._active_model_name = model_name
        # _model holds the loaded engine tuple (session, tokenizer); None until
        # the lazy load succeeds. This is what is_available() gates on, mirroring
        # the previous contract (review delta #5).
        self._model: tuple[Any, Any] | None = None
        self._tokenizer_only: Any = None
        self._backend: str | None = None
        self._dimensions: int | None = None
        self._load_error: BaseException | None = None
        self._load_lock = threading.Lock()
        self._encode_lock = threading.Lock()
        self._cache_folder = Path(
            os.environ.get("HF_HOME", "~/.cache/huggingface")
        ).expanduser()
        self._ram_threshold_mb = _int_from_env(
            "EMBEDDING_RAM_THRESHOLD_MB", DEFAULT_RAM_THRESHOLD_MB
        )
        self._onnx_file = os.environ.get("EMBEDDING_ONNX_FILE", _ONNX_FILE_DEFAULT)

        available_mb = _available_memory_mb()
        if available_mb is not None and available_mb < self._ram_threshold_mb:
            logger.warning(
                "Granite embedding may be RAM-constrained: available RAM %.0fMB is "
                "below the %dMB floor. The local engine requires more memory; see the "
                "hardware requirement (run `marvis doctor`). Proceeding with %s — no "
                "silent downgrade to a weaker model.",
                available_mb,
                self._ram_threshold_mb,
                model_name,
            )

    @property
    def active_model_name(self) -> str:
        """Return the active Granite model name."""

        return self._active_model_name

    @property
    def cache_folder(self) -> Path:
        """Return the Hugging Face cache folder used for the model snapshot."""

        return self._cache_folder

    @property
    def can_attempt_load(self) -> bool:
        """Return false only after a model load crash has been recorded."""

        return self._load_error is None

    @property
    def load_error_message(self) -> str | None:
        """Read-only last load error, for sanitized classification upstream (F1).

        Never surfaced raw to clients — only mapped to a leak-free enum.
        """

        return self._load_error

    def is_available(self) -> bool:
        """Return true once the model has loaded successfully.

        Eagerly warms the model (review delta #6): the first
        ``InferenceSession.__init__`` + a dummy run cost 1-3s; paying it here
        moves the cold-start out of the first real request. A load failure is
        swallowed (recorded in ``_load_error``) so callers can degrade to the
        keyword lane instead of crashing — matching the previous contract.
        """

        if self._model is not None and self._load_error is None:
            return True
        if self._load_error is not None:
            return False
        try:
            self._get_model()
        except Exception:  # noqa: BLE001 — recorded in _load_error, degrade gracefully
            return False
        return self._model is not None and self._load_error is None

    @property
    def dimensions(self) -> int:
        """Native output dimensions for Granite."""

        return self._dimensions or EXPECTED_DIMENSIONS

    def tokenizer(self) -> Any:
        """Return the loaded Rust ``tokenizers.Tokenizer`` (loads it if needed).

        Exposed for the Track 2 #4 prose chunker, which needs the SAME tokenizer
        the embedder uses to compute per-token char offsets — reusing it avoids a
        second tokenizer. The chunker only calls ``encode(text)`` (offsets), never
        the ONNX session, so chunking adds no forward pass of its own; the session
        is loaded here too only because both share the one lazy-load path.
        """

        _, tokenizer = self._get_model()
        return tokenizer

    def tokenizer_only(self) -> Any:
        """Return the Rust tokenizer WITHOUT loading the ONNX InferenceSession.

        The prose chunker needs only per-token char offsets, never a forward pass.
        In granite_remote the model lives in the sidecar, so loading the full
        session here would defeat the purpose (re-import onnxruntime + ~390MB into
        the tenant). This loads ONLY tokenizer.json (a few MB) at the pinned
        revision with the SAME truncation config the embedder uses -> identical
        offsets, so chunk boundaries never drift between the modes.
        """
        if self._tokenizer_only is not None:
            return self._tokenizer_only
        with self._load_lock:
            if self._tokenizer_only is not None:
                return self._tokenizer_only
            tokenizers_mod = importlib.import_module("tokenizers")
            hub = importlib.import_module("huggingface_hub")
            local = hub.snapshot_download(
                self._active_model_name,
                revision=MODEL_REVISION,
                cache_dir=str(self._cache_folder),
                allow_patterns=[
                    "tokenizer.json",
                    "tokenizer_config.json",
                    "special_tokens_map.json",
                    "config.json",
                ],
            )
            tokenizer = tokenizers_mod.Tokenizer.from_file(
                str(Path(local) / "tokenizer.json")
            )
            tokenizer.enable_truncation(max_length=MAX_LEN)
            self._tokenizer_only = tokenizer
            return self._tokenizer_only

    def embed_texts(
        self,
        texts: list[str],
        input_type: str = "document",
        batch_size: int = 16,
    ) -> list[list[float]]:
        """Embed texts and return L2-normalized native 384-dimensional vectors.

        ``input_type`` is a no-op (kept for interface compatibility): the model's
        ``config_sentence_transformers.json`` declares empty query/document
        prompts, so sentence-transformers adds no prefix either.
        """

        if not texts:
            return []

        self._get_model()
        try:
            raw_vectors = self._encode(texts, batch_size)
            vectors = [_normalize_l2(row) for row in raw_vectors]
        except Exception:
            logger.exception(
                "Granite embedding failed model=%s backend=%s input_type=%s",
                self._active_model_name,
                self._backend or "unknown",
                input_type,
            )
            raise

        if vectors:
            self._dimensions = len(vectors[0])
        return vectors

    def release_model(self) -> None:
        """Drop the warm ONNX session and ask libc to return freed arenas."""

        with self._load_lock:
            self._model = None
            self._backend = None
        _trim_native_allocator()

    def _get_model(self) -> tuple[Any, Any]:
        if self._model is not None:
            return self._model
        with self._load_lock:
            # Re-check under the lock: a concurrent caller may have loaded it.
            if self._model is not None:
                return self._model
            if self._load_error is not None:
                raise RuntimeError(
                    f"Embedding model load previously failed: {self._active_model_name}"
                ) from self._load_error

            start = time.perf_counter()
            try:
                session, tokenizer = self._load_onnx_session()
            except Exception as exc:
                self._load_error = exc
                logger.exception(
                    "Failed to load embedding model model=%s device=%s cache=%s",
                    self._active_model_name,
                    self.device,
                    self._cache_folder,
                )
                raise RuntimeError(
                    f"Embedding model unavailable: {self._active_model_name}"
                ) from exc

            self._model = (session, tokenizer)
            self._backend = "onnxruntime"
            self._dimensions = _session_output_dim(session) or EXPECTED_DIMENSIONS
            if self._dimensions != EXPECTED_DIMENSIONS:
                logger.warning(
                    "Granite ONNX output dim=%d != expected %d",
                    self._dimensions,
                    EXPECTED_DIMENSIONS,
                )
            logger.info(
                "Embedding model loaded model=%s backend=%s device=%s dims=%d "
                "onnx=%s cache=%s load_ms=%d",
                self._active_model_name,
                self._backend,
                self.device,
                self._dimensions,
                self._onnx_file,
                self._cache_folder,
                int((time.perf_counter() - start) * 1000),
            )
            return self._model

    def _load_onnx_session(self) -> tuple[Any, Any]:
        ort = importlib.import_module("onnxruntime")
        tokenizers_mod = importlib.import_module("tokenizers")
        hub = importlib.import_module("huggingface_hub")

        local = hub.snapshot_download(
            self._active_model_name,
            revision=MODEL_REVISION,  # pinned (delta #2)
            cache_dir=str(self._cache_folder),
            allow_patterns=_ALLOW_PATTERNS,
        )
        local_path = Path(local)

        tokenizer = tokenizers_mod.Tokenizer.from_file(str(local_path / "tokenizer.json"))
        tokenizer.enable_truncation(max_length=MAX_LEN)
        # F2: NO tokenizer padding. Padding is applied MANUALLY per planned batch in
        # _run_batch (to the batch max, not a global/encode-call max), so length-aware
        # bucketing actually bounds the padded sequence — and the quadratic-in-seqlen
        # attention memory. enable_padding would pad each encode_batch() to its call
        # max and defeat the single-pass tokenize + per-batch padding.

        sess_options = ort.SessionOptions()
        # intra_op = physical cores by default; inter_op=1 (single graph, batched).
        threads = _int_from_env("EMBEDDING_ORT_THREADS", _physical_cores())
        if threads > 0:
            sess_options.intra_op_num_threads = threads
        sess_options.inter_op_num_threads = 1
        sess_options.graph_optimization_level = (
            ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        )
        # F5: the CPU memory arena keeps ~2GB resident after a large
        # (4096-token) forward and never releases it, so a fleet-serving
        # sidecar creeps toward its cap. Opt-out (set ONLY in the sidecar
        # unit) frees that memory after each run: queries stay fast (tiny
        # tensors), only large doc embeds pay ~3x (rare, background). It is
        # an allocation strategy, not compute -> vectors are byte-identical.
        if os.environ.get("EMBEDDING_DISABLE_ARENA", "").strip().lower() in {
            "1", "true", "yes", "on",
        }:
            sess_options.enable_cpu_mem_arena = False
        # Persist the optimized graph next to the model so subsequent cold-starts
        # skip re-optimization (review delta #6). Best-effort: ignore if unwritable.
        try:
            opt_path = local_path / "onnx" / (
                Path(self._onnx_file).name + ".ort_optimized.onnx"
            )
            sess_options.optimized_model_filepath = str(opt_path)
        except Exception:  # noqa: BLE001
            pass

        session = ort.InferenceSession(
            str(local_path / self._onnx_file),
            sess_options=sess_options,
            providers=["CPUExecutionProvider"],
        )

        # Warm-up dummy run so the first real request does not pay graph init.
        try:
            self._run_session(session, tokenizer, ["warmup"])
        except Exception:  # noqa: BLE001 — warmup is best-effort
            logger.debug("Granite ONNX warm-up run failed (non-fatal)", exc_info=True)

        return session, tokenizer

    def _encode(self, texts: list[str], batch_size: int) -> list[list[float]]:
        if not texts:
            return []
        session, tokenizer = self._model  # type: ignore[misc]
        max_tokens = _int_from_env("EMBEDDING_MAX_TOKENS", _DEFAULT_MAX_TOKENS)
        token_budget = _int_from_env("EMBEDDING_TOKEN_BUDGET", _DEFAULT_TOKEN_BUDGET)
        max_count = max(1, batch_size)

        # Tokenize ONCE (tokenizer state is mutable + not thread-safe → under lock).
        # No tokenizer padding: we pad MANUALLY per batch in _run_batch.
        with self._encode_lock:
            encodings = tokenizer.encode_batch(texts)

        # Per-doc cap: measure len(ids) and slice. We do NOT toggle enable_truncation
        # per-call (it mutates shared tokenizer state under the lock); a doc over the
        # cap loses its tail (chunking is a deferred follow-up).
        ids_rows: list[list[int]] = []
        mask_rows: list[list[int]] = []
        truncated = 0
        for enc in encodings:
            eids = enc.ids
            emask = enc.attention_mask
            if len(eids) > max_tokens:
                eids = eids[:max_tokens]
                emask = emask[:max_tokens]
                truncated += 1
            ids_rows.append(eids)
            mask_rows.append(emask)
        if truncated:
            _warn_truncation(truncated, max_tokens)

        # Length-aware bucketing bounded by BOTH a token budget (count × seqlen) and
        # a max item count. _plan_batches REORDERS → scatter results back to input
        # order: callers rely on vectors[i] ↔ texts[i] (a silent permutation would
        # corrupt the index while per-string cosine tests still pass).
        lengths = [len(r) for r in ids_rows]
        out: list[list[float] | None] = [None] * len(texts)
        for group in _plan_batches(lengths, token_budget, max_count):
            vectors = self._run_batch(
                session,
                [ids_rows[i] for i in group],
                [mask_rows[i] for i in group],
            )
            for slot, vec in zip(group, vectors):
                out[slot] = vec
        # Every slot is filled: _plan_batches partitions all indices exactly once.
        return out  # type: ignore[return-value]

    def _run_session(
        self, session: Any, tokenizer: Any, chunk: list[str]
    ) -> list[list[float]]:
        import numpy as np  # local import keeps module import cheap

        # Tokenizer state (padding/truncation) is mutable + not thread-safe:
        # encode_batch under a lock; padding is to the max of THIS batch.
        with self._encode_lock:
            encodings = tokenizer.encode_batch(chunk)
        ids = np.array([e.ids for e in encodings], dtype=np.int64)
        mask = np.array([e.attention_mask for e in encodings], dtype=np.int64)
        hidden = session.run(
            ["last_hidden_state"],
            {"input_ids": ids, "attention_mask": mask},
        )[0]
        # CLS pooling — first token. NOT mean pooling (delta from the old client).
        cls = hidden[:, 0]
        return [[float(v) for v in row] for row in cls]

    def _run_batch(
        self,
        session: Any,
        ids_rows: list[list[int]],
        mask_rows: list[list[int]],
    ) -> list[list[float]]:
        """Run one pre-tokenized batch, padding to the batch max (not a global max).

        Inputs are already tokenized + capped. Right-padding with PAD_ID and
        attention_mask=0 leaves CLS pooling at position 0 unaffected, so a vector is
        identical regardless of which batch a doc lands in (cosine equivalence holds).
        """
        import numpy as np  # local import keeps module import cheap

        batch_max = max((len(r) for r in ids_rows), default=1) or 1
        n = len(ids_rows)
        ids = np.full((n, batch_max), PAD_ID, dtype=np.int64)
        mask = np.zeros((n, batch_max), dtype=np.int64)
        for row, (eids, emask) in enumerate(zip(ids_rows, mask_rows)):
            length = len(eids)
            if length:
                ids[row, :length] = eids
                mask[row, :length] = emask
        hidden = session.run(
            ["last_hidden_state"],
            {"input_ids": ids, "attention_mask": mask},
        )[0]
        cls = hidden[:, 0]
        return [[float(v) for v in row] for row in cls]


def _physical_cores() -> int:
    try:
        psutil = importlib.import_module("psutil")
        cores = psutil.cpu_count(logical=False)
        if cores:
            return int(cores)
    except Exception:  # noqa: BLE001
        pass
    return os.cpu_count() or 1


def _session_output_dim(session: Any) -> int | None:
    try:
        for output in session.get_outputs():
            if output.name == "last_hidden_state":
                shape = output.shape
                if shape and isinstance(shape[-1], int):
                    return int(shape[-1])
    except Exception:  # noqa: BLE001
        return None
    return None


def _available_memory_mb() -> float | None:
    try:
        psutil = importlib.import_module("psutil")
    except ImportError:
        logger.warning("psutil is not installed; skipping embedding RAM gate")
        return None
    return float(psutil.virtual_memory().available) / _MB


def _int_from_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("Invalid %s=%r; using %d", name, raw, default)
        return default


_truncation_warned = False


def _warn_truncation(count: int, max_tokens: int) -> None:
    """One-shot WARN when inputs hit the per-doc cap and were truncated (F2)."""
    global _truncation_warned
    if _truncation_warned:
        return
    _truncation_warned = True
    logger.warning(
        "Embedding: %d input(s) exceeded EMBEDDING_MAX_TOKENS=%d and were truncated; "
        "long docs lose tail context (chunking is a deferred follow-up).",
        count,
        max_tokens,
    )


def _trim_native_allocator() -> None:
    try:
        import gc

        gc.collect()
        if os.name != "posix":
            return
        import ctypes

        libc = ctypes.CDLL("libc.so.6")
        malloc_trim = getattr(libc, "malloc_trim", None)
        if malloc_trim is not None:
            malloc_trim(0)
    except Exception:  # noqa: BLE001 — allocator trimming is best-effort
        logger.debug("Native allocator trim failed", exc_info=True)


def _plan_batches(
    token_lengths: list[int],
    max_batch_tokens: int,
    max_batch_count: int,
) -> list[list[int]]:
    """Group input indices into batches bounded by BOTH a token budget and a count.

    The padded ONNX run allocates roughly ``len(batch) × max_seqlen_in_batch ×
    hidden`` (attention is quadratic in seqlen), so the bound that actually caps
    peak memory is the TOKEN BUDGET — ``len(batch) × max_seqlen ≤ max_batch_tokens``.
    The per-doc cap alone does not (a batch of all-long docs still co-batches).
    Length-aware: sorting by token length groups similar-length docs, collapsing the
    padding waste that drives the worst-case peak.

    Pure function (no model / no I/O) → unit-testable WITHOUT loading the model
    (learning a09b8754: never reproduce the real OOM on a shared host). Returns
    groups of ORIGINAL indices; callers MUST scatter results back to input order. A
    single doc above the budget still forms its own size-1 batch (never dropped) —
    the per-doc cap is what keeps that lone sequence bounded.
    """
    n = len(token_lengths)
    if n == 0:
        return []
    budget = max(1, max_batch_tokens)
    count_cap = max(1, max_batch_count)
    order = sorted(range(n), key=lambda i: token_lengths[i])
    batches: list[list[int]] = []
    cur: list[int] = []
    cur_max = 0
    for i in order:
        tl = max(1, token_lengths[i])
        cand_max = tl if tl > cur_max else cur_max
        if cur and (len(cur) >= count_cap or cand_max * (len(cur) + 1) > budget):
            batches.append(cur)
            cur = [i]
            cur_max = tl
        else:
            cur.append(i)
            cur_max = cand_max
    if cur:
        batches.append(cur)
    return batches


def _normalize_l2(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(value * value for value in vector))
    if norm == 0.0:
        return [0.0 for _ in vector]
    return [value / norm for value in vector]
