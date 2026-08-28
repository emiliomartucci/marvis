# OSS projection import preparation — awaiting final marvisx candidate

**Status: `awaiting_final_marvisx_candidate`** — 2026-08-27.

This document is the immutable record of the local preparation work (U1/U2 of
the [controlled engine synchronization plan, 2026-08-25]). Nothing in it was
applied to a shared area: the final synchronization waits for the definitive
marvisx candidate SHA. Machine-readable evidence of the pre-candidate dry run:
[`2026-08-27-precandidate-dry-run.json`](2026-08-27-precandidate-dry-run.json).

## Identities

| Identity | Value |
| --- | --- |
| OSS base (worktree branch `oss-projection-refresh-20260827`) | `07821c76b5e890c341eeed80ca2f4002ad482b79` (origin/main) |
| Engine pin at base | contract_version 1 @ `c02ee4adba4d5130be4ae6beeb43220c28986bde` |
| Pre-candidate exporter/source commit (NOT final) | `936e1e6e2753803b6e0074f95250e48fa01c77a9` (marvisx branch `fix/pr304-export-contracts`) |
| Pre-candidate payload digest | `632f02f94d6956f44aff39299adb0776b918e145d1e7b412e59ac0d1e6ff5867` (1186 files, 12 006 020 bytes) |
| Pre-candidate exporter identity digest | `874912009ee3f10f2070f3daeda1f4d3abfc0e9482dc900dae51a8a6fb0c9235` |
| Ownership map | `contracts/shared-ownership.yaml`, version 1 |

The pre-candidate was verified as a Git commit in an isolated temporary
repository (objects fetched read-only; no marvisx/Enterprise worktree was read
or modified) and the exporter was executed from that materialization with
source SHA = exporter SHA. It is a **pre-candidate only**.

## Deliverables (committed on the preparation branch)

- `contracts/shared-ownership.yaml` — reviewed ownership map (KTD2/KTD3):
  managed areas, OSS-owned areas, independent deny policy, never-delete.
- `scripts/import_shared_projection.py` — fail-closed importer/verifier:
  full source SHA mandatory; manifest and per-file hashes re-verified against
  payload bytes; payload digest recomputed with the exporter's exact record
  algorithm; allowlist/denylist enforcement; rejection of path traversal,
  absolute/backslash/non-NFC/control-character paths, symlinks (in bundle and
  destination), duplicate and case-colliding paths, unlisted bundle files,
  dirty destinations; dry-run/apply/rollback separated; byte-identical writes
  with readback; verifiable local rollback; deterministic, timestamp-free
  report with proposed tree digest (AE3).
- `scripts/test_import_shared_projection.py` — 37 tests: green bundle,
  determinism, tampered bytes/manifest, wrong profile, unsafe paths, secret
  markers/patterns, ownership collisions (including oss-owned-wins-over-
  managed ordering), migration-history rewrite, unguarded Cloud/Enterprise
  imports, apply/rollback cycle, dirty-worktree and symlinked-destination
  refusal.

## U1 inventory (OSS surface at `07821c7` vs pre-candidate payload)

- 912 files already synchronized byte-identically (last mirror lineage).
- 163 files would be overwritten by the engine update inside managed areas.
- 107 files are additions, including the whole governed contract surface
  (`contracts/openapi/`, `contracts/actions/`) that the OSS tree does not
  carry yet.
- OSS-owned and protected: `apps/desktop-ui`, top-level `scripts/` validators,
  `contracts/surfaces/`, `contracts/engine-pin.yaml`, `contracts/desktop-host.yaml`,
  `core/api/tests/`, `core/cli/tests/`, `core/api/console_dist/`, `docs/decisions/`.
- Local-only inside managed areas (survive every import, never deleted):
  `core/api/mcp/tools/pull_requests.py`, `core/api/mcp/tools/storage.py`,
  `core/api/mcp/tools/workspace.py`, `core/api/services/workspace_tools.py` —
  copies from the mirror era that the public/shared policy now excludes
  upstream; their long-term ownership is an explicit decision.
- Licenses: `LICENSE`, `THIRD_PARTY_LICENSES`, `CHANGELOG.md`, `SECURITY.md`,
  `MANIFEST.in` are byte-identical in the payload (no license drift). The
  package stays BSL-1.1 source-available; no OSI claim is introduced.
- Observation for Plan C (pre-existing, not caused by the import):
  `pyproject.toml` `[project.urls]` points at `github.com/emiliomartucci/marvisx-oss`
  while the repository remote and the surface registry say `emiliomartucci/marvis`.

## Pre-candidate dry run (NOT applied)

Exit status `blocked` (exit 2), report digest `1f09ed63cf061847632aedc0b396f8b3af3934ec95598fa22a77cce1c6db8fe3`,
deterministic across repeated runs from the clean preparation commit
(`68830a53…`, worktree dirty entries 0 — one unique digest). Proposed tree
digest: `12c72b2bd2c7eb7bd4b854d91b8696016a52bab006bc45d99b84ff4160e8245d`.

- 1182/1186 files importable; 0 deny violations (no secrets, no forbidden
  paths/suffixes/components, no unguarded Cloud/Enterprise imports).
- 5 guarded optional seams reported (explicit, KTD10-conformant):
  `core/api/routers/auth.py:307,601,693` (`workos`),
  `core/api/use_cases/projects.py:115,156` (`core.hosted_lifecycle.state`).
- Migrations: 210/210 tracked OSS migrations byte-identical in the payload,
  0 changed, 0 absent; 47 new migrations appended (155–179). Forward-only
  history holds.
- Contract window: candidate `contracts/openapi/VERSION` = contract_version 3
  (predecessor 2) vs pinned 1. The pin advances only after compatibility
  gates pass (U4), never during import.

### Blocked collisions — decisions required before any apply

1. `.github/workflows/release.yml` — the payload's workflow builds the shared
   Console from `core/console` and drops the three OSS validator gates; the
   OSS workflow builds `apps/desktop-ui` and runs them
   (`.github/workflows/release.yml:35-64`; registry rule
   `contracts/surfaces/desktop-ui.yaml` forbids marvisx UI bundles).
   Recommendation: permanent OSS ownership; Plan A should stop carrying it.
2. `pyproject.toml` — engine pin `fastmcp>=2` → `fastmcp>=3.4.2,<3.5`
   (`pyproject.toml:106`) and a new `marvis-graph-export` entry point
   (`pyproject.toml:145-147,190`). Packaging is OSS-owned; the dependency
   move is engine-driven and needs an explicit transfer decision plus a
   dependency-compat gate run.
3. `README.md` — one substantive line (`README.md:97`): OSS documents
   "91 tools … pull requests"; upstream removes repository-lifecycle tools
   from the public claim. Content decision at import.
4. `.gitignore` — adds `!core/api/console_dist/icons/*.png`
   (`.gitignore:24`), tied to the shared Console build. Minor; decide with #1.

## Compatibility gates executed

| Gate | Result |
| --- | --- |
| Surface registry / local perimeter / desktop host validators | PASS (3× valid) |
| Validator + importer unit tests (`unittest discover -s scripts`) | PASS (37 new + existing) |
| Console launcher characterization (`pytest core/cli/tests`) | PASS (25 passed) |
| Package build from post-import overlay (outside the worktree) | PASS (`marvisx_cli-0.4.0` wheel + sdist) |
| Wheel install in a fresh venv + `marvis --help` | PASS |
| `core.api.main` / `core.api.mcp.stdio` import smoke | PASS |
| API suite at OSS base | 364 passed / 38 failed / 4 errors — reproduces the plan's 2026-08-25 baseline exactly |
| API suite on post-import overlay | 348 passed / 54 failed / 4 errors → **16 new failures caused by the pre-candidate engine** |
| Negative importer tests (tampered/excluded content) | PASS (see deliverables list) |
| Cloud/Enterprise dependency scan | CLEAN (deny scan + guarded-seam AST scan) |

### The 16 new failures (classified; all are final-candidate/U4/U5 work, none are importer defects)

1. `core/api/routers/finder.py:60-62` — `_authenticated_actor` calls itself:
   infinite recursion (6 failures, `test_share_edit.py`). **Upstream defect;
   returns to Plan A** per the plan's dependency rule.
2. Production `Settings` now require an explicit JWT secret ≥ 32 UTF-8 bytes
   (3 failures, `test_config_env_aliases.py`). Deliberate hardening; the local
   runtime profile needs a decision on secret provisioning before import.
3. OSS-side test fixtures build the pre-import schema; the engine's newer
   schema adds `workspace_id` / `project_id` columns (2 + 4 failures,
   `test_audit_permissions.py:27`, `test_kg_trust_write_path.py`). Test-side
   reconciliation at import (U5).
4. `core/api/tests/test_safety_bridge.py:291` — unknown-rule-key advisory
   semantics changed (1 failure). Policy change to ratify in U4.

## Exact commands for the final candidate

```bash
# From a clean checkout of the branch carrying this preparation:

# 1. Dry-run (no worktree mutation; non-zero exit means blocked/violations)
python3 scripts/import_shared_projection.py \
  --bundle <final-bundle-dir> \
  --expected-source-sha <FINAL-40HEX-SOURCE> \
  --expected-exporter-sha <FINAL-40HEX-EXPORTER> \
  --report docs/projection/<date>-final-candidate-dry-run.json

# 2. Apply — only after the blocked-collision decisions above are resolved
#    (ownership-map change or upstream payload fix) and the worktree is clean.
#    The backup directory MUST live outside the repository.
python3 scripts/import_shared_projection.py \
  --bundle <final-bundle-dir> \
  --expected-source-sha <FINAL-40HEX-SOURCE> \
  --expected-exporter-sha <FINAL-40HEX-EXPORTER> \
  --mode apply \
  --backup-dir <dir-outside-repo> \
  --report docs/projection/<date>-final-candidate-apply.json

# 3. Verified local rollback (restores the exact pre-apply tree digest)
python3 scripts/import_shared_projection.py \
  --bundle <final-bundle-dir> \
  --expected-source-sha <FINAL-40HEX-SOURCE> \
  --mode rollback \
  --backup-dir <same-dir-outside-repo>
```

The importer performs no network I/O: everything it verifies comes from the
local bundle bytes, the local Git object database, and the ownership map.
