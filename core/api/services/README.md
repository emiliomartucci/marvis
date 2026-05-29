# Core API Services

## Embedding Backends

`core.api.services.embedding_service` is the stable public module used by
routers for semantic search and indexing. Select the backend with
`EMBEDDING_MODE`:

- `voyage`: default and backward-compatible path. Uses Voyage AI
  `voyage-4-lite` with 512-dimensional output.
- `granite_local`: self-hosted in-process embeddings through
  `GraniteEmbeddingClient`, backed by
  `ibm-granite/granite-embedding-97m-multilingual-r2` and SentenceTransformers.
  ONNX is attempted first, with torch fallback if ONNX is unavailable.
- `granite_remote`: reserved for Phase 2 sidecar Docker mode. The current W1
  implementation raises `NotImplementedError`.
- `dual`: computes both Voyage and local Granite for pre-cutover validation,
  then returns Voyage embeddings for existing ranking behavior.

Granite-97m R2 is the W1 compliance alternative to Voyage for EU-sensitive
ship paths. The decision follows the May 22, 2026 audit at
`/data/projects/marvisx/docs/audits/2026-05-22-m2-3-qwen3-cax41-recall-benchmark.md`:
Qwen3-0.6B was rejected on the CAX41 ARM target because missing bf16 hardware
support made throughput unusable, while Granite-97m kept acceptable recall and
latency in a small Apache 2.0 model.

Baseline from the audit: Granite-97m on CAX41 ARM measured about 8.2 doc/s for
document pooling and 26.6 doc/s for query embedding, enough for a full 15K
corpus re-embed in roughly 30 minutes.

`vec_documents` remains a sqlite-vec `float[512]` virtual table for W1. Granite
and MiniLM fallback vectors are native 384-dimensional and are zero-padded to
512 only at the `embedding_service` boundary before serialization. This is
pragmatic for W1 ship and avoids a migration; a Phase 2 route can add a
`vec_documents_granite float[384]` shadow table when backend-specific storage is
worth the added routing complexity.
