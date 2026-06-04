# Marvis

[![PyPI](https://img.shields.io/pypi/v/marvisx-cli.svg)](https://pypi.org/project/marvisx-cli/) [![License: BSL 1.1](https://img.shields.io/badge/license-BSL%201.1-blue.svg)](LICENSE) [![Change Date: Apache-2.0 2030](https://img.shields.io/badge/change%20date-Apache--2.0%20(2030)-lightgrey.svg)](LICENSE)

**The Company Brain your agents install.** Use Claude Code, or whatever coding agent you run, to manage projects and coordinate your agents, not just write code. Marvis captures what your agents do as they work, structures it into a cross-project Knowledge Graph, and hands it back to them, so your work stays on track and the system learns from it. Self-hosted, EU-resident, with a tamper-evident audit log.

**Open-core and free to run.** Marvis is the free core: you self-host it, read the source, fork it, audit it, at no cost. MarvisX is the commercial tier on top of the same brain: multi-department teams, data sovereignty and managed hosting, and autonomous, hands-off-the-keyboard operation. Marvis is what you install today; MarvisX is what you reach for when a single self-hosted instance is no longer enough.

## Install

Marvis runs as a CLI your agent drives. Install it, then let the agent you already use take it from there:

```bash
uv tool install marvisx-cli
marvis init
```

`marvis init` scaffolds the project and installs the governance hooks, then exposes the MCP surface to your agent. Python 3.10–3.13; on 3.14, pin the interpreter: `uv tool install --python 3.13 marvisx-cli`. No `uv` yet? `curl -LsSf https://astral.sh/uv/install.sh | sh`.

**Managed Windows (application-control policy)?** If your organization enforces application control — Windows **WDAC** / AppLocker / Managed Installer, common on managed corporate machines — Windows blocks running any binary placed in `%LOCALAPPDATA%`, which is where `uv` installs tools, so `uv tool install` can't execute. Don't bypass it. Install Marvis inside **WSL2** (recommended) or **Docker**, or ask IT to whitelist `uv` (or publish it through your managed channel); the Linux runtime is identical.

To run the full Console + API stack yourself, see [Self-hosting the full stack](#self-hosting-the-full-stack).

## What it does, in plain terms

- **Your agents stop forgetting.** Decisions, specs, playbooks, learnings and mistakes are captured as a side effect of the work your agents already do, not retyped by hand.
- **Your work stays on track.** A reflective Brain layer compares what agents produce against your intent and surfaces drift early, before it compounds across sessions.
- **One brain, many projects and teams.** A cross-project Knowledge Graph links decisions, code and documents, so an agent working in one project can see what was decided in another.
- **Governed by default.** Constitution rules enforced at the hook level, plus a foreign-key-immutable audit log: every action traces back to the human who approved it.
- **It lives in your terminal, not a dashboard.** An agent-native MCP surface keeps your existing Claude Code / Codex / Cursor as the place you work.

## Under the hood

- **Cross-project Knowledge Graph** with 17 deterministic edge types covering code, work-chain artifacts, knowledge-chain documents, cross-project references, and bridge edges. Canonical IDs follow `{prefix}:{kind}:{slug}`.
- **Append-only audit log** (foreign-key + trigger enforced) with provenance pinned at the SQLite level. The managed **MarvisX** tier builds on it to cover the logging surface required by EU Regulation 2024/1689 (AI Act) Article 12.
- **Brain reflection pipeline** in five layers — substrate, digest and journal, drift checker, memory operations, findings — closed by a Direction-Aware loop.
- **Constitution-enforced safety rules** enforced by deterministic hook gates and an MCP mirror for providers without native hooks.
- **Agent-native MCP surface** exposing 91 tools, callable identically from Claude Code, Codex, Gemini, OpenCode, and a web console.

## Search and embedding quality

Marvis defaults to **IBM Granite-Embedding-97m-Multilingual-R2** self-hosted via ONNX, blended with SQLite BM25 keyword search through Reciprocal Rank Fusion. Backend is selectable through `EMBEDDING_MODE`.

This is a deliberate tradeoff: EU residency, self-hosting, and zero external embedding cost come at the cost of retrieval quality that is below commercial managed embedding providers. See [`docs/SEARCH-QUALITY.md`](docs/SEARCH-QUALITY.md) for the benchmark numbers, affected workflows, mitigations available today, and the Phase 2 fine-tune roadmap.

## License posture

- **Free to self-host and use internally**, including for commercial purposes. Code is **source-available** under the **Business Source License 1.1**; a paid license is required only to offer Marvis to third parties as a hosted or managed service. The change date automatically converts the code to **Apache License 2.0** four years after release.
- Documentation and architectural patterns: **CC-BY 4.0**.

This is open-core, not OSI open source: the core is free to run, read and fork, and the BSL holds back only competing commercial-as-a-service use until the change date. GitHub does not recognize the BSL and labels the repo `NOASSERTION`; the authoritative terms are in [`LICENSE`](LICENSE).

## Architecture map

The system groups capabilities into eight functional domains (`M1 Capture`, `M2 Index`, `M3 Retrieve`, `M4 Reflect`, `M5 Act`, `M6 Agent-native I/O`, `M7 Compliance`, `M8 Productization`). The Knowledge Graph stitches them together with canonical IDs and an audit chain.

The full architectural description is the MarvisX preprint on Zenodo: [`10.5281/zenodo.20341860`](https://doi.org/10.5281/zenodo.20341860).

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

## Self-hosting the full stack

The CLI above runs Marvis against a local SQLite database. To run the full Console + API stack yourself, use the reference Docker Compose deployment. Clone the repository, copy `deploy/_template/.env.example` to `.env`, populate the required secrets, and bring the stack up:

```bash
cp deploy/_template/.env.example deploy/_template/.env
cd deploy/_template/
docker compose up -d
```

The Console becomes available on the configured `CONSOLE_PORT` and the API on the configured `API_PORT`. Detailed first-run instructions are inside the deployment template.

## MCP surface

Marvis ships an MCP server exposing 91 tools across 11 categories: project and session context, tasks, handoffs, semantic search, learnings, costs, pull requests, knowledge graph queries, ingest triage, brain reflection layer, and audit-and-monitoring controls.

The same surface is used by autonomous agents and by humans through the web console. The intent is parity: any action a user can take through the console, an agent can also take through MCP, and the reverse.

## Single-user local runtime (`MARVIS_OSS_LOCAL`)

The MCP server runs in-process: it calls the same use_cases the HTTP API calls, against the local SQLite database, with no uvicorn and no Node bridge. In this single-user mode there is no HTTP API in front of the database, so the HTTP audit chokepoint (which in the managed deployment forces every mutation through the API) does not exist.

Audit is still recorded: the extracted use_cases write the audit log themselves (`core/api/use_cases/{tasks,pull_requests,audit}.py`), so a mutation issued through MCP still produces an audit row at the use_case level. What is lost is only the additional HTTP-surface chokepoint.

Setting `MARVIS_OSS_LOCAL=1` (truthy: `1`/`true`/`yes`/`on`) makes the `block-db-direct-write` safety rule **advisory** — direct writes to a Marvis SQLite database are allowed with a warning instead of being blocked. When the variable is unset or falsey the rule is unchanged: direct DB writes are blocked fail-closed, exactly as in the managed deployment. This trade-off is intentional for single-user self-hosting and never weakens the default.

**No-fork guarantee.** The HTTP API and the MCP server are not two implementations: both are thin adapters over the same `core/api/use_cases`, differing only in how they fill the per-call `CallerContext`. The proof is the API test suite staying green after the extraction — the same behaviour, exercised through the HTTP surface, over the identical use_cases the MCP server calls.

## Telemetry

Marvis sends anonymous, opt-out usage telemetry to help improve it: coarse event counts plus version and OS buckets, tied to a random install id (a plain UUID, never derived from your machine, user, or email). It never sends file content, project data, or personal information.

Turn it off at any time:

```bash
marvis telemetry off
```

Or per environment: `MARVIS_TELEMETRY=0`, the universal `DO_NOT_TRACK=1`, or `telemetry: false` in `~/.marvis/settings.yaml`. With `MARVIS_TELEMETRY=log`, Marvis prints exactly what it would send to stderr and transmits nothing.

## Documentation

- [`docs/SEARCH-QUALITY.md`](docs/SEARCH-QUALITY.md) — search quality expectations, affected workflows, and mitigations

## Contributing

Issues and bug reports are welcome through the repository tracker.

## References

- Preprint (Zenodo): [`10.5281/zenodo.20341860`](https://doi.org/10.5281/zenodo.20341860)
- License (code): Business Source License 1.1, change date converts to Apache License 2.0 four years after release
- License (documentation): CC-BY 4.0
