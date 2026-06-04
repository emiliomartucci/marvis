# Changelog

All notable changes to this project are documented here.

This project uses strict Semantic Versioning: `MAJOR.MINOR.PATCH`.

## [Unreleased]

_No unreleased changes._

## [v0.3.7] - 2026-06-04

> Shipping first as the pre-release **0.3.7b1** for beta testers
> (`uv tool install 'marvisx-cli==0.3.7b1'`); stable installs stay on 0.3.6 until
> the final 0.3.7.

The "credibility and onboarding" release: the flagship command no longer hangs,
the fresh schema and the wizard are clean, telemetry is opt-in, the license and
the README say what is actually true — and a new `marvis guide` teaches an agent
how to organize a repository the Marvis way.

### Added
- `marvis guide` — one callable, OSS-clean reference for how Marvis works: concepts (programs › projects › tasks), naming, directory layout, the Knowledge Graph edge types, tags, the frontmatter spec, the Brain, the lifecycle, and how to adopt the shape on an existing repo. `--section` / `--list` / `--json` (`4f838447`).
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

The "make it alive" release: a clean OSS install now reflects, schedules, and
keeps its search index fresh on its own, with explicit consent, and installs on
Windows.

### Added
- `marvis brain run --mode free|full` — run one Company-Brain reflection cycle on demand. `free` is the no-LLM upkeep floor (never spends a BYOK key); `full` adds LLM journal polish (`6f25847`).
- Opportunistic background reflection — at most once per day, fired on any `marvis` invocation, in a detached process that never blocks the command. Off until enabled at install (`ce07cf0`).
- `marvis brain schedule --enable|--disable|--status` — install an OS-native daily reflection timer (macOS launchd / Linux systemd `--user` with linger + catch-up / cron fallback). `--status` reports the real OS state (`4c02eb6`).
- `marvis init` now asks three consent questions — reflection cost-mode (free/full/off), an opt-in autonomy timer, and governance-hook install — and wires the answers. Non-interactive runs default everything off (`a4e2934`).
- Self-healing search — a first query over an empty index returns `index-building`, kicks off a background build, and a retry returns results; the auto-build defers when free RAM is below a safe floor so it never OOMs a laptop (`75ba5c4`).
- BYOK → brain — the provider key collected at `marvis init` is wired to the reflection gateway at runtime for OpenAI-compatible providers (OpenAI, Anthropic, a self-hosted gateway), so `full` reflection works without a manual env export. Managed deployments keep their env-configured gateway (`3716632`).
- Deploy: populate OSS deploy template (`de2dcf2`).
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
