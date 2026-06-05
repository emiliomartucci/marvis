# Search Quality

MarvisX-OSS Phase 1 uses self-hosted embeddings by default: IBM
Granite-Embedding-97m-Multilingual-R2 running in-process through ONNX, blended
with SQLite BM25 keyword search through Reciprocal Rank Fusion (RRF).

The Granite engine runs the pre-exported ONNX graph directly via `onnxruntime`
plus the Rust `tokenizers` package — torch-free (no `sentence-transformers` /
`optimum` / `torch`), which keeps `uv tool install marvisx-cli` under ~150MB of
dependencies. Pooling is CLS (`last_hidden_state[:, 0]`) then L2-normalize, fp32,
pinned to a fixed model revision so vectors stay reproducible.

This page sets expectations for that setup. The short version: Phase 1 chooses
EU compliance, self-hosting, and zero external embedding cost over peak managed
provider retrieval quality.

## What to expect

For the Phase 1 default setup, the current expected quality band is:

| Setup | Expected Jaccard@5 / overlap vs commercial managed | Practical reading |
|---|---:|---|
| Granite-97m R2 only | 0.31 measured in the 2026-05-22 audit | Around 1.5 of the same top-5 documents |
| Granite-97m R2 + BM25 + RRF | 0.41-0.46 estimated after W2 hybrid retrieval | Around 2.0-2.3 of the same top-5 documents |
| Commercial managed embedding target | 0.85+ target quality band | Managed-provider class retrieval |

The 0.41-0.46 number is an estimate for the Phase 1 hybrid stack, not a claim
that a post-W2 evaluation already measured exactly 0.45. It combines the
measured Granite baseline with the expected lift from BM25 keyword retrieval and
RRF blending.

What this means operationally:

- Your top result may still be right, especially for queries with strong
  technical keywords.
- The full top-5 list will differ more often than it would with a commercial managed model.
- Increasing `top_k` matters more with Granite than with a larger commercial model.
- Search should be treated as a candidate finder, not as a perfect semantic
  oracle.

Internal benchmark reference:
`docs/audits/2026-05-22-m2-3-qwen3-cax41-recall-benchmark.md`.

## Why is it different from managed providers?

The gap is structural.

Granite-Embedding-97m-Multilingual-R2 is a 97M parameter open model. It is small,
fast enough for CPU hosting, Apache 2.0 licensed, and practical for OSS users who
do not want a managed embedding dependency.

A commercial managed embedding model is trained at much larger scale, on a
multi-TB corpus, with provider-side infrastructure and model iteration hidden
behind an API.

The 2026-05-22 audit showed the same shape across open models, benchmarked
against a commercial managed baseline:

| Model | Pool throughput | ETA for 15K corpus | Jaccard@5 vs managed | Spearman vs managed |
|---|---:|---:|---:|---:|
| Qwen3-Embedding-0.6B | 0.01 doc/s | 412 hours | 0.2719 | -0.2210 |
| Granite-97m R2 | 8.22 doc/s | 30 minutes | 0.3075 | -0.1207 |
| Granite-311m R2 | 1.66 doc/s | 150 minutes | 0.3574 | +0.0268 |

Quality improved with model size, but remained far from managed-provider
ranking. The audit conclusion was that open sub-500M models are not drop-in
replacements for commercial managed embeddings on the MarvisX mixed technical
corpus.

Phase 1 therefore does not promise managed-provider parity.

## What works well

Search works best when the query contains unambiguous technical anchors:

- Acronyms: `RRF`, `FTS5`, `KG`, `BYOK`, `ADR`.
- Process names: `brain cycle`, `ingest classifier`, `similar_to threshold`.
- Code or configuration names: `EMBEDDING_MODE`, `SEARCH_RRF_K`,
  `vec_documents`, `mcp__pir__search`.
- Error phrases and specific symptoms.
- Commit scopes, plan IDs, task IDs, or migration numbers.

In these cases, the BM25 leg often recovers precision that the smaller embedding
model would miss. RRF then lets keyword and semantic matches reinforce each
other without requiring either signal to be perfect.

Common conceptual queries also work when the concept appears in multiple
documents:

- "why did we choose self-hosted embeddings"
- "recent search quality tradeoffs"
- "project memory consolidation"
- "deployment safety rules"

For broad themes, the embedding leg is still useful because there are usually
several acceptable documents.

## What does not work well

Expect weaker behavior for:

- Rare or highly specific queries where there is exactly one correct document.
- Abstract concepts without technical keywords.
- Queries that depend on internal vocabulary the base model has not learned.
- Queries where the right answer is a short note buried inside a long handoff.
- Near-duplicate documents where small wording differences matter.
- Cross-language queries that mix Italian operational shorthand with English
  code terms and no exact anchors.

The most fragile case is: "there is one perfect document, it uses project-local
language, and the query describes it abstractly." A larger commercial managed
model handles this class much better than Granite-97m base.

## Affected flows

The embedding backend is not only used by a search box. Lower retrieval quality
affects every workflow that depends on semantic neighbors.

### `mcp__pir__search`

This is the most visible path. It searches across tasks, projects, files,
handoffs, learnings, and related artifacts. With Granite Phase 1, ask for more
results when recall matters:

```text
mcp__pir__search(q="similar_to threshold calibration", top_k=15)
```

If the tool or client wrapper does not expose `top_k`, use a more specific query
with technical anchors.

### `check_learnings`

`check_learnings` is used before risky actions such as deploys, migrations,
pushes, dependency upgrades, or structural refactors.

Phase 1 risk: a relevant learning may fall outside the top-5 if the query is too
abstract. Mitigation: include the module path, exact error, command, migration
name, or provider name in the query.

### KG `similar_to` edges

The Knowledge Graph uses semantic similarity to create `similar_to` edges between
artifacts. Granite uses a calibrated threshold tuned for its own embedding scale:

```env
KG_SIMILAR_TO_THRESHOLD_GRANITE_97M=0.85
```

The threshold intentionally favors precision over edge volume.

### Brain digest cross-project retrieval

Brain digest and cross-project memory flows use retrieval to connect related
events, decisions, handoffs, and findings. Phase 1 may miss weaker semantic
connections unless they share explicit terms.

### M1 ingest classifier

Ingest classification can use retrieved context to route or label new artifacts.
With Granite Phase 1, labels based on exact project terms, known acronyms, and
existing tags are more reliable than labels based only on abstract similarity.

## Mitigations available today

### Increase top-K from 5 to 15

For agent workflows, the main mitigation is to request more candidates:

```text
top_k=15
```

The top-5 overlap is lower, but one of the top-15 is much more likely to contain
the relevant document. This is the default recommendation for critical checks.

### Hybrid retrieval is active by default

W2 added BM25 + embedding retrieval through RRF. This is the main Phase 1
quality mitigation:

```env
SEARCH_BM25_ENABLED=true
SEARCH_RRF_K=60
```

BM25 is especially helpful when the query includes exact technical terms.

### Salience boost is available

Important documents can be boosted through:

```text
mcp__pir__boost_document
```

Use this for canonical ADRs, runbooks, post-mortems, or project documents that
should win ties and appear more often in recall sets.

### Model-aware thresholds are calibrated

W3 added model-aware thresholds for KG `similar_to` edges. Granite uses its own
stricter value rather than a generic default.

This reduces noisy semantic edges and makes graph retrieval less sensitive to
the embedding scale.

## Roadmap Phase 2

Phase 2 item M2.7 is "Embedding fine-tune periodic".

The committed scope is a corpus customization loop:

- Extract training triples from the local PiR corpus and KG relationships.
- Fine-tune the Granite embedding head against project-local vocabulary.
- Evaluate against a holdout query set before swapping the model.
- Swap atomically with rollback to the previous ONNX artifact.
- Trigger periodically through the Brain cycle hook.

Expected quality lift: +0.10 to +0.20 Jaccard over the Phase 1 Granite baseline.

Effort estimate: 1-2 weeks of development for the fine-tune pipeline plus the
Brain cycle hook. This is not part of Phase 1 ship.

## If you need higher retrieval quality

Phase 1 ships local Granite embeddings as the default and only self-hosted
backend. If you need higher retrieval quality immediately and accept an external
dependency, the `EMBEDDING_MODE` dispatch is designed to make room for a managed
embedding backend.

That path trades away the EU-compliant, self-hosted posture: it sends embedding
payloads to an external service, so it is a no-go for deployments that must keep
all document text in-region. It is not the OSS default.

For EU-resident managed quality, the Phase 2 direction is a bring-your-own-key
(BYOK) managed pool such as Mistral EU. That is a different tradeoff: EU residency
improves, but the embedding layer is still not self-hosted.

## Honest tradeoff

MarvisX-OSS Fase 1 prioritizes EU compliance + self-hosting + zero external cost
over peak retrieval quality. If you need commercial-managed quality with EU
residency, wait for the Phase 2 BYOK pool or run a hybrid setup.

The Phase 1 system is usable when queries include technical anchors, when agents
request more candidates for critical checks, and when canonical documents are
boosted. It is not a transparent substitute for a commercial managed embedding
provider.
