# MarvisX

Self-hosted, EU-resident **company brain** for agent-native operations: a cross-project Knowledge Graph, audit-immutable provenance, reflective Brain pipeline, and an agent-native Model Context Protocol surface.

Released as a defensive-publication architectural pattern. See the Zenodo preprint: [`10.5281/zenodo.20341860`](https://doi.org/10.5281/zenodo.20341860).

## What MarvisX gives you

- **Cross-project Knowledge Graph** with 17 deterministic edge types covering code, work-chain artifacts, knowledge-chain documents, cross-project references, and bridge edges. Canonical IDs follow `{prefix}:{kind}:{slug}`.
- **Foreign-key-immutable audit log** with provenance pinned at the SQLite level. Designed to satisfy the logging surface required by EU Regulation 2024/1689 (AI Act) Article 12.
- **Brain reflection pipeline** in five layers — substrate, digest and journal, drift checker, memory operations, findings — closed by a Direction-Aware loop.
- **Constitution-enforced safety rules** enforced by deterministic hook gates and an MCP mirror for providers without native hooks.
- **Agent-native MCP surface** exposing 91 tools, callable identically from Claude Code, Codex, Gemini, OpenCode, and a web console.

## Search and embedding quality

MarvisX-OSS Phase 1 defaults to **IBM Granite-Embedding-97m-Multilingual-R2** self-hosted via ONNX, blended with SQLite BM25 keyword search through Reciprocal Rank Fusion. Backend is selectable through `EMBEDDING_MODE`.

This is a deliberate tradeoff: EU compliance, self-hosting, and zero external embedding cost come at the cost of retrieval quality that is below commercial managed embedding providers. See [`docs/SEARCH-QUALITY.md`](docs/SEARCH-QUALITY.md) for the benchmark numbers, affected workflows, mitigations available today, and the Phase 2 fine-tune roadmap.

## License posture

- Code: **Business Source License 1.1**, with an automatic change date converting to **Apache License 2.0** four years after release.
- Documentation and architectural patterns: **CC-BY 4.0**.

The combination is intended to make the architecture freely citable while preserving the ability to ship a self-hosted reference deployment.

## Architecture map

The system groups capabilities into eight functional domains (`M1 Capture`, `M2 Index`, `M3 Retrieve`, `M4 Reflect`, `M5 Act`, `M6 Agent-native I/O`, `M7 Compliance`, `M8 Productization`). The Knowledge Graph stitches them together with canonical IDs and an audit chain.

For the full architectural description, see the Zenodo preprint linked above.

## Repository layout

```
core/
├── api/        # FastAPI service: KG, ingest, brain, MCP transport
├── console/    # Next.js web console
├── kb/         # Project-local knowledge base templates
├── mcp-pir/    # MCP server (stdio) exposing the 91-tool surface
└── scripts/    # Deployment, migration, and operational scripts

deploy/
└── _template/  # Reference deployment template
                # (Docker Compose + environment scaffolding)
```

## Quick start

The reference deployment uses Docker Compose. Clone the repository, copy `deploy/_template/.env.example` to `.env`, populate the required secrets, and bring the stack up:

```bash
cp deploy/_template/.env.example deploy/_template/.env
cd deploy/_template/
docker compose up -d
```

The Console becomes available on the configured `CONSOLE_PORT` and the API on the configured `API_PORT`. Detailed first-run instructions are inside the deployment template.

## Setting up local embeddings

[`docs/MIGRATION-TO-LOCAL-EMBEDDINGS.md`](docs/MIGRATION-TO-LOCAL-EMBEDDINGS.md) covers the step-by-step path to the self-hosted Granite backend, including first-boot setup, re-indexing an existing corpus, and KG threshold recalibration.

## MCP surface

MarvisX ships an MCP server exposing 91 tools across 11 categories: project and session context, tasks, handoffs, semantic search, learnings, costs, pull requests, knowledge graph queries, ingest triage, brain reflection layer, and audit-and-monitoring controls.

The same surface is used by autonomous agents and by humans through the web console. The intent is parity: any action a user can take through the console, an agent can also take through MCP, and the reverse.

## Single-user local runtime (`MARVIS_OSS_LOCAL`)

The MCP server runs in-process: it calls the same use_cases the HTTP API calls, against the local SQLite database, with no uvicorn and no Node bridge. In this single-user mode there is no HTTP API in front of the database, so the HTTP audit chokepoint (which in the managed deployment forces every mutation through the API) does not exist.

Audit is still recorded: the extracted use_cases write the audit log themselves (`core/api/use_cases/{tasks,pull_requests,audit}.py`), so a mutation issued through MCP still produces an audit row at the use_case level. What is lost is only the additional HTTP-surface chokepoint.

Setting `MARVIS_OSS_LOCAL=1` (truthy: `1`/`true`/`yes`/`on`) makes the `block-db-direct-write` safety rule **advisory** — direct writes to a Marvis SQLite database are allowed with a warning instead of being blocked. When the variable is unset or falsey the rule is unchanged: direct DB writes are blocked fail-closed, exactly as in the managed deployment. This trade-off is intentional for single-user self-hosting and never weakens the default.

**No-fork guarantee.** The HTTP API and the MCP server are not two implementations: both are thin adapters over the same `core/api/use_cases`, differing only in how they fill the per-call `CallerContext`. The proof is the API test suite staying green after the extraction — the same behaviour, exercised through the HTTP surface, over the identical use_cases the MCP server calls.

## Documentation

- [`docs/SEARCH-QUALITY.md`](docs/SEARCH-QUALITY.md) — search quality expectations, affected workflows, and mitigations
- [`docs/MIGRATION-TO-LOCAL-EMBEDDINGS.md`](docs/MIGRATION-TO-LOCAL-EMBEDDINGS.md) — setting up the self-hosted Granite embedding backend
- [`docs/solutions/architecture-patterns/`](docs/solutions/architecture-patterns/) — architectural decision records

## Contributing

This is a defensive-publication release. The repository accepts issues for bug reports and clarifications. The architecture itself is documented in the Zenodo preprint; the codebase is intended as the reference implementation of that architecture.

## References

- Preprint (Zenodo): [`10.5281/zenodo.20341860`](https://doi.org/10.5281/zenodo.20341860)
- License (code): Business Source License 1.1, change date converts to Apache License 2.0 four years after release
- License (documentation): CC-BY 4.0
