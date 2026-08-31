# Changelog

All notable changes to this project are documented here.

This project uses strict Semantic Versioning: `MAJOR.MINOR.PATCH`.

## [Unreleased]

No changes yet.

## [0.4.3] - candidate

Recovery candidate for the same reviewed public/shared source as 0.4.2. It
keeps product behavior unchanged and repairs only the immutable release path.

### Fixed

- Isolated unit tests from both CI tag variables and real tags already present
  in the shared Git object store.
- Checked out the immutable tag before failed-release containment so GitHub can
  verify the existing tag without creating or moving one.
- Recorded every failed immutable version explicitly; 0.4.1 and 0.4.2 cannot
  be reused.

This section describes a release candidate only. Version 0.4.3 is not
published until the separate tag, PyPI and post-publication acceptance gates
all succeed.

## [0.4.2] - 2026-08-31 (failed, not published)

Security and provenance refresh based on the exact merged Plan B product base
and the separately pinned CI evidence foundation from PR #61. The release
keeps the local CLI, MCP, hooks and packaged GUI contracts while updating the
shared engine and its regression coverage.

### Security

- Hardened shared identity, authorization, throttling and audit boundaries.
- Bound the package to deterministic public/shared source provenance.

### Changed

- Removed retired Heypocket, n8n and Reddit runtime integrations; historical
  database migrations and temporary compatibility tombstones remain.
- Expanded Python 3.10-3.13, Linux, macOS and Windows install and upgrade proof.

### Attempted fixes

- Isolated release unit tests from live GitHub tag variables so a tag run does
  not accidentally enter publication mode.
- Made failed GitHub Release lookups branch on the API exit status instead of
  treating an error payload as an existing final release.

The immutable tag exists, but the tag workflow stopped before the package
build. Unit tests were isolated from CI variables but still observed the real
tag in the shared Git object store. Failed-release containment then lacked a
Git checkout. No GitHub Release or PyPI file was created. Version 0.4.2 is
burned and must never be reused.

## [0.4.1] - 2026-08-31 (failed, not published)

The immutable tag exists, but the tag workflow stopped before the expensive
build because unit tests inherited the live tag context. No GitHub Release or
PyPI file was created. Version 0.4.1 is burned and must never be reused.

## [0.4.0] - 2026-06-19 (tentativo fallito, non pubblicato)

Il tag e il record GitHub di questo tentativo esistono, ma PyPI non ha mai
ricevuto la versione. Le note sotto descrivono il contenuto candidato storico,
non una release consegnata o installabile.

### Added
- **`marvis console` (plumbing)** — a local-GUI launcher: ensures the local API is up and opens the browser on the locally served UI. The release pipeline now builds the Console static export and ships it in the wheel; the public GUI launch follows the redesign. Groundwork: the local API can mount and serve a static UI same-origin (CSP kept same-origin).
- **Claude Code plugin marketplace (in-repo)** — `/plugin marketplace add` support: the repo now ships a plugin that carries the MCP server, governance hooks and starter skills into Claude Code in one command, with a guard against double MCP registration for users who also ran `marvis mcp install`. The runtime still comes from `pip install marvisx-cli` (local) — the plugin is the plug, not the power plant.
- **Remote MCP endpoint (hosted tier plumbing)** — `MARVIS_MCP_TRANSPORT=http` serves the MCP tool surface over streamable-http on `127.0.0.1:$MARVIS_MCP_PORT/mcp` with per-tenant Bearer auth (`TENANT_BEARER_TOKEN` required). The default stdio transport is unchanged and stays unauthenticated-local. Adds `fastmcp>=2` as a dependency (imported only on the HTTP path) and raises `mcp>=1.27.0`.
- **Tenant provisioning lifecycle** — `core/scripts/provision_tenant.py` + systemd/Caddy templates under `deploy/hosted-tenants/` for per-tenant instances (operator tooling; inert unless invoked).
- **Hosted onboarding automatico** — tenant provisioning and local runtime defaults now produce a usable first-run flow instead of requiring manual repair.
- **Search ready on empty tenants** (#37) — fresh hosted tenants can search/index without pre-existing project content or manual bootstrap.

### Fixed
- **Console QA fixes** (#30-#36) — 10 fixes across the local Console surface:
  brand-correct PWA icons and manifest colors, ProjectNavigator fidelity,
  BYOK provider cleanup, local-tier Teams/RBAC hiding, completed Todos, Universe
  project visibility, and automatic encryption-secret provisioning.

## [v0.3.9b6] - 2026-06-17 (beta)

Hosted onboarding + local-GUI QA fixes, all merged to main. (b5 shipped the
console/plugin/hosted-tier groundwork above; b6 makes a fresh tenant and the
local GUI actually work end-to-end.)

### Fixed — hosted tenant onboarding (dogfooding a real tenant)
- Provisioning no longer leaves a tenant broken: the data tree is owned by the
  service user (chown fails loud instead of silently, when it can't), and a
  per-tenant `settings.yaml` + `MARVIS_SETTINGS_PATH` make `project list`/import/
  MCP see the projects (env alone didn't set `projects_root`).
- Local embedding works on a fresh tenant: `HF_HOME` points at a shared Granite
  cache that is created + writable (the systemd sandbox no longer makes
  onnxruntime's optimized-graph write fail → `embedding_unavailable`); the cache
  can be pre-warmed by `setup-server`.
- Imported `project.yaml` with a machine-specific `repo_path` no longer warns/
  breaks — code-KG is skipped cleanly when the path is absent.
- Per-tenant `MemoryMax` raised so a Granite reindex isn't OOM-killed.

### Fixed — local GUI QA
- BYOK "Add provider key" works out-of-the-box: the local tier auto-provisions
  the encryption secret instead of failing closed (#36).
- Universe/Cosmo shows registered projects even before code indexing (#35).
- Todos gained a "Completed" view, so finished items have a destination (#34).
- BYOK dropdown drops the personal "Mac Gateway" option (use OpenAI-compatible)
  (#32); Teams + Roles & Permissions are hidden on the local single-user tier
  (#33).

## [v0.3.9b4] - 2026-06-13 (beta)

Beta-feedback fixes, round 3 — bundles round 2 plus the four bugs the
GUI QA on b2 surfaced. b3 was cut but never published; b4 replaces it
so the Mac retest installs everything in one go.

### Fixed (already in the unpublished b3)
- Local console no longer empty (#17, #18, #20 + migration 154).
- Todo classification works without an LLM (#22 — heuristic fallback).
- Design fidelity: project color on the kanban + todos as list rows
  (#19, #21).

### Fixed (new in b4)
- Internal project links now apply the /ui basePath (#23) — the
  project dashboard was unreachable by clicking.
- Decidi gate writes a durable ADR + supersede chain + audit row
  (#25) — confirming a `decidi` todo now persists the artefact under
  `<project>/docs/decisions/`.
- "Esegui il brain ora" no longer polls forever (#26) — the page
  tracks the triggered manual run by id, jumps to its `cycle_key` on
  success, and surfaces a 2-minute timeout / failure message.
- Journal `what_changed` references are hydrated against
  `/brain/events` (#27) — no more "Aggiornamento senza titolo /
  Senza progetto" when the underlying events have both.

Beta for internal testing — install only with an explicit pin:
`pip install marvisx-cli==0.3.9b4`.

## [v0.3.9b3] - 2026-06-12 (beta)

Beta-feedback fixes, round 2 — the "empty local console" trio plus todos
value on the no-LLM tier:

### Fixed
- **Console no longer empty on the local tier** (#17, #18, #20): the
  loopback-only Local Operator had an empty visibility set (every task and
  project filtered out); the work-project/demo root was hardcoded to
  `/data/projects` (onboarding demo 400 on macOS); and a legacy seeded
  `project_dirs` row overrode the configured `projects_root` at startup
  (migration 154 removes it).
- **Todos classified even without an LLM provider** (#22): deterministic
  heuristic fallback (type from "idea"/"decidere se"/imperative openers,
  FU from dates and weekdays, project auto-link by slug/name) — defaults
  never made worse; honest capture caption.
- **Design fidelity** (#19, #21): kanban cards show the project color
  (hex values from the DB were being rejected) with the mock's dot + name
  layout; todos render as list rows with a checkbox / typed gate-dot and
  inline link actions instead of boxed cards.

Beta for internal testing — install only with an explicit pin:
`pip install marvisx-cli==0.3.9b3`.

## [v0.3.9b2] - 2026-06-12 (beta)

Beta-feedback fixes, round 1 (first Mac test of the redesigned GUI):

### Fixed
- **Console skin no longer falls back to the old blue theme on non-English
  browsers** — the locale hook hydrated differently than the prerendered HTML
  (React #418) and the recovery dropped the `.theme-v2` class; the class is now
  server-rendered and the first paint is locale-deterministic.
- **Exo 2 fallback font 404** — CSS font URLs now carry the `/ui` base path.
- **PWA icons 404** — `icon-192/512` were silently dropped between repo and
  wheel; they ship again (manifest resolves).
- **Brand lockup** — sidebar now shows the bot mark + "marvis · CONSOLE"
  (the old lockup embedded a second bot mark).
- **First project invisible after `marvis init`** — the API launched by
  `marvis console` now applies `storage.projects_root` from settings.yaml, so
  `/api/v1/projects` lists the project immediately (#B1).
- **macOS: `/api/v1/sessions` 500 on every load** (#15) — portable runtime dir
  instead of the Linux-only `/run/user/{uid}`; missing tmux degrades to an
  empty session list.
- **Stale API serving the wrong brain on relaunch** (#16) — `/health` exposes a
  DB fingerprint, the launcher restarts a mismatched API via a new pidfile, and
  `marvis console --stop` stops the local API explicitly.

Beta for internal testing — install only with an explicit pin:
`pip install marvisx-cli==0.3.9b2`.

## [v0.3.9b1] - 2026-06-12 (beta)

First wheel with the redesigned local Console GUI (code-complete 2026-06-12):
local-mode sidebar (Diario · Todos · Task · Progetti · Universe), unified todos
queue with typed actions and approval adapter, 5-step onboarding wizard with
Casa Lorenzi demo data and spotlight tour, project dashboards with persisted
colors and manual relations, Universe graph view, IT+EN interface. `marvis
guide`/`marvis doctor` now explain the full GUI mechanism. Beta for internal
testing — install only with an explicit pin: `pip install marvisx-cli==0.3.9b1`.

## [v0.3.8] - 2026-06-10

The "measured memory" release: we benchmarked our own retrieval against a
plain-files agent across 314 judged runs — and shipped the fixes the data
demanded. With the engine flags on, Marvis now wins the largest category
outright (facts that changed over time: it returns the CURRENT value, with the
evidence span inline) and never invents when it doesn't know. A default install
still behaves like v0.3.7 — every engine feature below is opt-in behind a flag.
Also in this release: install completeness in `marvis doctor`, a human `marvis
task` surface, and the Brain on your Claude Code subscription with zero API key.

### Added
- **Evidence spans in search results**, behind `MARVIS_SEARCH_SPANS` (requires `MARVIS_CHUNKING` for the write side): the semantic lane max-pools chunk-level matches into the document ranking and each file-backed hit carries the winning chunk's text expanded to line boundaries ±12 lines (`span_text` / `span_path` / `span_line_start` / `span_line_end`), so an agent can answer FROM the search result without a follow-up file read. Additive fields, `null` when the flag is off or the hit is row-backed; fail-soft when the source file moved. Chunking now also runs on the generic document upsert and the reindex paths (previously a single write path), keeping the chunk sidecar fresh.
- **Orchestration-ready MCP tools.** New `project_impact(slug)` — the project-level blast radius ("what blocks if I pause/close this project"), distinct from the code graph. The graph tool descriptions are rewritten for an agent acting as a cross-project orchestrator (dropped the `[Power-user]` gating and the "use get_project instead" steering), the MCP server ships an `instructions` decision tree (task-type → tool), and `session_brief` suggests `project_impact` on cross-project work. The cold-start core is marked `alwaysLoad` so tool-search clients defer the rest of the ~70-tool surface.
- Engine groundwork toward 0.3.8 — all behind default-off flags, so a default install behaves exactly like v0.3.7. Opt-in pieces: prose chunking before embedding, a structural Knowledge-Graph lane in search ranking, span-grounded citations in the session brief, deterministic community detection over the graph, and bitemporal columns on `learnings` (`valid_from` / `invalid_at`).
- A read-time **trust & freshness** surface on graph reads, behind `MARVIS_TEMPORAL_MEMORY`: each neighbour carries its edge `derivation` (observed vs inferred) and a `needs_review` flag when its facts are stale (missing or future timestamps are never treated as fresh), bitemporal node columns (`last_verified_at`, `superseded_by`), and tools to mark a node verified or superseded. The per-neighbour flag is gated as a minority hint — when the index is broadly stale it is suppressed and the count is stated once in the summary, so a stale slice does not drown an unrelated query.
- Relation-typed **answer-ready claims** on the impact tools, behind `MARVIS_KG_CLAIMS`: `project_impact` / `graph_impact` add a `claims[]` block where the database has already counted dependents *by edge relation* — a true dependency (`depends_on` / `imports` / `calls`) is reported separately from a weak association (`refers_to` / `mentions`, surfaced as `mentioned_by` and never a dependency). The agent quotes the server-computed number instead of re-counting the raw edge list. Additive (existing fields unchanged).
- **Onboarding completion state.** `marvis doctor` now reports whether the install is *complete*, as binary done/not-done states split into **required** (CLI on PATH, config valid, MCP server registered) and **recommended** (governance hooks, a project imported, indexed code, the brain, an LLM). "100%" is the required ones only, so opting out of the brain never pins you below complete. `marvis doctor --json` gains an additive `onboarding_completion` block; `marvis guide` documents the same states (a test keeps the two in sync). The exit code is unchanged — completeness is orthogonal to health errors.
- **`marvis task` from the terminal.** A human surface for tasks (previously agent/MCP-only): `marvis task list [--status open|all|<status>]`, `marvis task show <id>`, `marvis task approve <id>`, `marvis task reject <id>` — `--json` on the read commands. In single-user the human running the CLI is the approval gate; against a remote multi-user backend the human-only 403 is shown as actionable guidance.
- **Run the Brain on your Claude Code subscription — no API key** (`BRAIN_LLM_PROVIDER=claude_cli`). A new Brain LLM backend that calls `claude -p` (headless) as a subprocess instead of an HTTP gateway, so `marvis brain run` produces polished narrative with zero API key. Default stays `gateway` (unchanged). Pure generation (no tools), fail-soft: any error/timeout degrades transparently to the deterministic baseline.

### Fixed
- **`marvis doctor --json` is an ARRAY again** (kept the documented contract): a beta briefly wrapped the check list in an object, breaking every consumer that iterates it — caught by the Windows E2E probe. The onboarding-completion block now rides as one additive array element (`name: "onboarding_completion"`, full payload under `summary`); a parity test pins the response contract.
- **Evidence spans were always `null` for MCP/HTTP consumers** (`0.3.8b6` regression, caught by an external acceptance run): the search engine attached `span_text`/`span_path`/`span_line_*` to its hits, but the response builder dropped them when constructing the API model — with the flags on and chunks populated, every consumer saw `null` spans. The fields are now propagated, and a parity test pins the response contract so a model field can no longer ship half-wired.
- **Full-text search lost document bodies on every write** (behind `MARVIS_FTS_BODIES`, default off): the `documents_fts` sync triggers stored the file *path* instead of the body, so any document written or updated after the one-time migration backfill was invisible to keyword search on its content — keyword ranking systematically favoured OLD document versions. With the flag on, every document write now refreshes the full-text row with the real title+body in the same transaction; `core/scripts/backfill_documents_fts.py` repairs historical rows (idempotent, batched).
- `create_learning` no longer fails with `table learnings has no column named valid_from` on a brain upgraded from `<=0.3.7`: pending migrations now apply on every entry point (CLI / MCP / brain, not just `marvis init`), and `valid_from` is written only under the temporal flag. The migration-016 admin seed also no longer aborts a fresh-DB boot when no admin password is configured — single-user installs skip the seed instead of crashing. Closes #12.
- `marvis init` (and `project create` / `import`) accept single-character slugs again — the wizard regex required a trailing character. Closes #10.
- With `MARVIS_TEMPORAL_MEMORY` on, `session_brief` no longer surfaces superseded learnings in its cold-start bundle. Closes #14.
- `graph_impact` / `graph_neighbors` now report index freshness (indexed sha vs current HEAD) instead of a confident-looking but possibly stale blast radius; `marvis doctor` flags projects indexed without git provenance. Closes #13.

### Known limitations
- `project_impact` and project-level graph reasoning need a **populated** Knowledge Graph. On a fresh install the graph is empty — `marvis project import` registers a project but does not index its topology (`import != index`), so `project_impact` returns "node not found" until you run `marvis project index <slug>` and cross-project dependency edges exist. The orchestration benefit is real once the graph is populated; out of the box it is not there yet. Auto-populating the graph on import (or degrading `project_impact` to the brief's file view) is a planned follow-up.

_Iterated through betas `0.3.8b1`-`0.3.8b7` (packaging validated on a fresh-install cross-OS matrix; retrieval validated by a 314-cell blind-judged benchmark). Install: `pip install marvisx-cli` (or `uv tool install marvisx-cli`)._

_Note: `0.3.8b2` was **yanked** — a stale wheel built before the migration-016 fix reached PyPI (which is write-once) and crashed a fresh-DB boot. Use `0.3.8`._

## [v0.3.7] - 2026-06-04

The "credibility and onboarding" release: the flagship command no longer hangs,
the fresh schema and the wizard are clean, telemetry is opt-in, the license and
the README say what is actually true — and a new `marvis guide` teaches an agent
how to organize a repository the Marvis way.

### Added
- `marvis guide` — one callable, public-package-clean reference for how Marvis works: concepts (programs › projects › tasks), naming, directory layout, the Knowledge Graph edge types, tags, the frontmatter spec, the Brain, the lifecycle, and how to adopt the shape on an existing repo. `--section` / `--list` / `--json` (`4f838447`).
- Enriched `projects/_template` scaffold — the full mold an agent copies onto a repo: `context.md`, `memory/handoff.md`, worked `docs/{brainstorms,plans,solutions}` examples, and `input`/`output` READMEs, each with its frontmatter shown (`7c0d9671`).
- `marvis init` and `marvis doctor` now point your agent at an existing folder and tell it to adopt it non-destructively via `marvis guide` (`ee174dbf`).
- `marvis brain enable` / `disable` / `status` — a supported toggle for the Brain (`brain_enabled`), so moving it out of shadow mode no longer requires a direct DB write that the governance hooks block. Closes #7.

### Changed
- Telemetry is now **opt-in** — off by default; `MARVIS_TELEMETRY` is a symmetric on/off override and `DO_NOT_TRACK` still wins. No install id is coined before consent (`daac9ac4`).
- The `marvis init` wizard and its option help are fully English (`9d5eedbd`).
- `marvis doctor` reports the resolved `settings.yaml` paths instead of the module defaults (`9d5eedbd`).
- License: the BSL Additional Use Grant is widened (Option A) — free to self-host and use internally, including commercially; a paid license is required only to offer Marvis to third parties as a competing hosted/managed service (`09b5ddd3`).
- README: the EU AI Act / audit-at-scale claim is scoped to the managed MarvisX tier; added a license section + BSL badges (`31e0e243`).
- `marvis init` and `marvis project import` now spell out "register vs index" and hint `marvis project index <slug>`, so a fresh, un-indexed knowledge graph no longer looks broken. Closes #8.
- `marvis project index` builds the structural graph (calls/imports/defines) by default; per-symbol code embeddings are now opt-in behind `--embed`. `search` does not query code embeddings yet, so the default run no longer pays the model-load / token cost for data nothing reads (`--no-embed` stays accepted as a back-compat no-op). Closes #9 (`a9b7f90e`).

### Fixed
- `marvis brain run` no longer hangs ~30s — the one-shot CLI closes the aiosqlite pool it opens (the writer is a non-daemon thread the interpreter joins at exit). Closes #1 (`b5f3acf8`).
- The fresh schema no longer ships 17 stale `graph_*_backup_NNN` snapshot tables (~13% of a clean DB). Closes #3 (`b3f2449e`).
- The Brain now reflects from the standalone runtime: `marvis brain run` and `brain_cycles_recompute` register their source collectors lazily, so capture→reflect produces events instead of silently collecting zero (the collectors used to register only under the API service). Closes #6.
- `marvis project import` / `create` slugify the project slug, so a directory like `BP VPP` no longer becomes a non-conformant slug that `project list` silently hides (an empty result hard-errors with a hint to pass `--slug`). Closes #5.

## [v0.3.6] - 2026-06-03

The "runs on Windows" release: the full local runtime — CLI, reflection,
scheduling — now installs and runs natively on Windows, no WSL or Docker. Plus a
notify-only update check, an MCP tool to revise learnings in place, and the
cross-OS test matrix moved to free public CI.

### Added
- Windows-native runtime — a clean `uv tool install marvisx-cli` now runs on Windows: paths resolve under `%LOCALAPPDATA%\marvisx` (platformdirs), the console reconfigures to UTF-8, and `marvis project list` no longer crashes with `No module named pwd` (`675c8e77`, `4ec497a0`).
- `marvis brain schedule` on Windows — a native daily-reflection timer via Task Scheduler (`schtasks`), matching the macOS launchd / Linux systemd backends; `--status` reports the real OS state (`4a298802`).
- Notify-only update check — `marvis` tells you when a newer version is on PyPI and never auto-installs (`8b7bef5f`).
- `update_learning` MCP tool — revise an existing learning in place, with re-embed so search reflects the edit (`45fffe22`).

### Changed
- OS-seam boundary — every OS-varying call in `core/cli` + `core/wizard` routes through the new `core/platform` module, enforced by a CI gate, so a forgotten platform guard cannot silently crash a Windows box (`1f3f4cfd`, `87130a44`).
- Cross-OS E2E CI — the expensive macOS/Windows matrix now runs free on the public mirror; the private source keeps a cheap ubuntu+windows pre-merge gate (`617a9d08`).
- Scripts read `MARVIS_*` environment variables with a `PIR_*` fallback (rebrand, additive — nothing breaks) (`e4163880`).

### Fixed
- Packaging: ship `core.platform` in the wheel so the runtime imports on a fresh install (`31ff15ba`).
- Deploy smoke now asserts the running server has the configured database open, so a misconfigured path fails closed instead of serving an empty database (`ac3a0528`).

## [v0.3.5] - 2026-06-03

The "make it alive" release: a clean local install now reflects, schedules, and
keeps its search index fresh on its own, with explicit consent, and installs on
Windows.

### Added
- `marvis brain run --mode free|full` — run one Company-Brain reflection cycle on demand. `free` is the no-LLM upkeep floor (never spends a BYOK key); `full` adds LLM journal polish (`6f25847`).
- Opportunistic background reflection — at most once per day, fired on any `marvis` invocation, in a detached process that never blocks the command. Off until enabled at install (`ce07cf0`).
- `marvis brain schedule --enable|--disable|--status` — install an OS-native daily reflection timer (macOS launchd / Linux systemd `--user` with linger + catch-up / cron fallback). `--status` reports the real OS state (`4c02eb6`).
- `marvis init` now asks three consent questions — reflection cost-mode (free/full/off), an opt-in autonomy timer, and governance-hook install — and wires the answers. Non-interactive runs default everything off (`a4e2934`).
- Self-healing search — a first query over an empty index returns `index-building`, kicks off a background build, and a retry returns results; the auto-build defers when free RAM is below a safe floor so it never OOMs a laptop (`75ba5c4`).
- BYOK → brain — the provider key collected at `marvis init` is wired to the reflection gateway at runtime for OpenAI-compatible providers (OpenAI, Anthropic, a self-hosted gateway), so `full` reflection works without a manual env export. Managed deployments keep their env-configured gateway (`3716632`).
- Deploy: populate the public self-hosted deploy template (`de2dcf2`).
- Infra: OOM mitigation — `user-1000.slice` MemoryHigh=20G / MemoryMax=24G drop-in + idempotent installer (`f47b9d0a`).

### Fixed
- Packaging: drop the unconditional `uvloop` dependency so `uv tool install marvisx-cli` succeeds on Windows; `uvicorn[standard]` still pulls uvloop on Linux/macOS, where it's supported (`1fe3284`).
- Git-ops: PR merges always create a merge commit (`--no-ff`) so generated release notes capture every PR rather than dropping fast-forwarded ones (`e90c556`).
- Hooks: point safety bridge to core scripts (`3acab45`).
- License: replace AGPL PDF parser fallbacks (`a7702ea`).

## [v2.0.0] - 2026-05-18

### Changed
- Materialized the final core layout for the Plan 4 switch path.
- Preserved the existing annotated unsigned `v2.0.0` tag for backward compatibility.

### Fixed
- Added frontmatter to the final audit report.

## [v1.0.0] - 2026-05-11

### Changed
- Historical baseline before the Plan 4 release train. Detailed entries were not backfilled.
