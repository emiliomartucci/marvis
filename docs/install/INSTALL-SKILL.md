---
title: Install MarvisX (agent-executed)
audience: [agent]
visibility: external
slice: 1
commands:
  - marvis --version
  - marvis doctor
  - marvis init
  - marvis project import
  - marvis mcp register
  - marvis hooks install
  - marvis status
  - marvis hooks status
  - marvis mcp status
golden_rule: never hand-edit settings.json / .mcp.json / hooks — always use the merge primitives
---

# Install MarvisX

You are an agent installing MarvisX into the user's environment. Read this whole
document, then execute it top to bottom. Every operational step is marked:

- **[DETERMINISTICO: invoke command]** means run the command exactly as written,
  replacing only explicit placeholders such as `<project-path>`.
- **[GIUDIZIO: you decide]** means inspect the local state and choose within the
  stated bounds. Do not invent extra setup steps.

The golden rule, true at every step: **NEVER hand-write `settings.json`, `.mcp.json`,
or hook files. NEVER overwrite an existing config. Always use the `marvis ...` merge
primitives.** They are deterministic, idempotent, and back up before they write.

---

## 1. What MarvisX is

MarvisX gives your agent persistent memory, governance (rules it cannot violate),
and a knowledge graph linking tasks, code, and decisions. It runs 100% locally as a
single-process Python program. No mandatory account, no phone-home, no marketing.

## 2. What you get after install

- The PiR **MCP server** attached to the agent (memory + KG tools).
- **Governance hooks** active on Claude Code (block unsafe writes, enforce task-first).
- The user's **projects registered** so the agent can see them.
- The `marvis` CLI: `marvis status`, `marvis brief`, `marvis triage`, `marvis approve`,
  `marvis audit`.

## 3. Prerequisites

Verify before doing anything else. Native Windows is out of slice 1; on Windows,
use WSL and follow the Linux path.

- **Python 3.10–3.13** (the local Granite engine's onnxruntime has no 3.14 wheel on every platform yet; on Python 3.14 use `uv tool install --python 3.13 marvisx-cli`), plus `bash`, `git`, `sqlite`.
- **Install the CLI — gold standard: `uv tool install marvisx-cli`.** `uv` (Astral)
  installs the `marvis` command in its own isolated environment and manages the Python
  interpreter for you — no "which Python?", no polluting the user's environment. If `uv`
  is missing, install it first (`curl -LsSf https://astral.sh/uv/install.sh | sh`), then
  `uv tool install marvisx-cli`. Use `uv tool install` (persistent), NOT `uvx` (ephemeral).
  - **Fallback:** `pip install marvisx-cli` also works, but it binds `marvis` to whichever
    Python `pip` belongs to — prefer `uv` on an unknown machine.
  - Until the public release, install from the mirror/repo path the user gives you. The
    one-line public installer (`curl … | sh`) and the PyPI package ship with the public release.
- **Docker is NOT required to run MarvisX.** Docker is only for the maintainers' smoke
  test / CI. Do not ask the user to install Docker.

**[DETERMINISTICO: invoke command]** Run:

```bash
python3 --version
```

If Python is below 3.10, **STOP and report**. Do not proceed on an older
interpreter.

**[DETERMINISTICO: invoke command]** Ensure `uv` exists:

```bash
command -v uv || curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"
uv --version
```

If `uv --version` still fails after the installer, **STOP and report** the shell
and OS. Do not switch to `pip` unless the recovery table in §10 says to.

**[DETERMINISTICO: invoke command]** Install the CLI:

```bash
uv tool install marvisx-cli
export PATH="$HOME/.local/bin:$PATH"
command -v marvis
marvis --version
```

Until the public PyPI release, replace `marvisx-cli` with the exact local wheel,
sdist, or repository path the user provided. Do not guess a Git URL.

**[DETERMINISTICO: invoke command]** Pre-check the install:

```bash
marvis doctor
```

If `marvis doctor` exits non-zero, apply the printed fix exactly once, then run
`marvis doctor` again. If it still exits non-zero, **STOP and report the exact
failing check**. Do not hand-edit config files as a workaround.

---

## 4. The protocol you MUST follow: scan → interview → install+adapt

This is the core. Execute the three phases in order. Each step is idempotent: if the
thing is already done, skip it.

### Phase A — Scan (read-only; use your file-reading tools)

**[GIUDIZIO: you decide]** classify the current folder. Read, do not write:

1. Is there a `.git` here? How many sub-directories contain `.git` or `project.yaml`?
   Is there already a `.claude/`? → classify the folder as **fresh**, **single-repo**,
   or **multi-project**.
2. Read the user's Claude Code install to learn what NOT to clobber:
   - `.claude/settings.json` — existing hooks? cozempic? custom hooks?
   - `.mcp.json` — MCP servers already registered?
   - `CLAUDE.md` — existing project instructions?

Record what you found. You will preserve all of it.

### Phase B — Interview (only where needed; never aspirational)

**[GIUDIZIO: you decide]** ask ONLY these, and only when ambiguous:

1. Show what you found in the scan and **confirm which projects to onboard**.
2. Confirm `projects_root` if it is ambiguous.
3. BYOK provider (reuse `marvis init` step 3 — anthropic / openai / mac_gateway / skip).
4. **Telemetry: inform, do not ask.** It is anonymous and default-ON. Tell the user it
   is on and how to turn it off (`MARVIS_TELEMETRY=0`). Do not request permission —
   it is opt-out, not opt-in (see §8).

Do not ask aspirational questions (compliance regions, license frameworks, future
features). Ask only what changes the next install step.

### Phase C — Install + adapt (invoke the primitives, MERGE)

Run these in order. Each is idempotent — re-running is safe.

0. **[DETERMINISTICO: invoke command]** Run the pre-write health check:
   `marvis doctor`
   If it exits non-zero, stop Phase C and follow §10. Do not write project,
   MCP, hook, or Claude config until the CLI install itself is healthy.
1. **[DETERMINISTICO: invoke command]** Bootstrap or import projects:
   - If **fresh**: `marvis init` (the wizard writes `settings.yaml` + seeds the first project).
   - Otherwise, for **each confirmed project**: `marvis project import <path>`.
     If a `project.yaml` already exists for that path, the import is a no-op — skip it.
2. **[DETERMINISTICO: invoke command]** Register the MCP server:
   `marvis mcp register`
   (merges the `pir` entry into `.mcp.json`, preserving every other server). If `pir`
   is already present and correct, it is a no-op.
3. **[DETERMINISTICO: invoke command]** Install the governance hooks:
   `marvis hooks install`
   (merges the hook entries into `.claude/settings.json`, preserving every non-Marvis
   entry such as cozempic). If already installed, it is a no-op.
4. **[GIUDIZIO: you decide]** Adapt `CLAUDE.md`: if there is no MarvisX section,
   **append** one describing the `marvis` commands. If the user already has a `CLAUDE.md`,
   **never overwrite it** — append only. This is the one non-safety step where you use
   judgement; keep your addition short and additive.
5. **[DETERMINISTICO: invoke command]** Verify (see §7).

> Optional, before any write: run any `marvis ... register`/`install` with `--dry-run`
> first to show the user the exact diff. Use this if the user wants to see what changes.

---

## 5. Decision-tree onboarding

Match the folder you classified in Phase A and run the matching primitives. If
already-installed, the upgrade path is OUT of this slice (§9) — the primitives stay
idempotent, so re-running them does not break anything.

| Folder you detected | What to run |
|---|---|
| **Fresh / empty** | `marvis init` (wizard → `settings.yaml` + first project) → `marvis mcp register` → `marvis hooks install`. |
| **Single git repo** | `marvis project import .` (it reads `repo_path` from `.git`, writes `project.yaml`) → `marvis mcp register` → `marvis hooks install`. |
| **Multi-project workspace** | For each sub-dir the user confirmed: `marvis project import <path>`. Then `marvis mcp register` → `marvis hooks install` once. |
| **MarvisX already installed** | Reconcile/upgrade is **OUT of slice 1**. Tell the user. The primitives are idempotent, so re-running `marvis hooks install` / `marvis mcp register` will not clobber — they no-op. Do not attempt a deep upgrade. |

---

## 6. The primitives you invoke (do NOT reinvent)

These commands are deterministic and idempotent. They parse the target file, merge
ONLY their own keys, back up to a timestamped `.bak`, and write atomically. **Use them
instead of editing config by hand.**

| Command | What it does |
|---|---|
| `marvis init` | Interactive bootstrap wizard: writes `settings.yaml`, seeds the first project. |
| `marvis project import <path>` | Registers a project (reads `repo_path` from `.git` for code projects), writes `project.yaml`. No-op if already registered. |
| `marvis mcp register [--config PATH] [--dry-run]` | Merges the `pir` MCP entry into `.mcp.json` (`command` = the resolved interpreter, `args` = `["-m","core.api.mcp.server"]`). Preserves every other server. No-op if already correct. |
| `marvis hooks install [--dry-run]` | Merges the governance hook entries into `.claude/settings.json` + copies the hook scripts. Preserves every non-Marvis entry. No-op if already installed. |
| `marvis status` | Health check of the local MarvisX install. |
| `marvis mcp status` | Reports whether `pir` is registered AND responds (a real `tools/list` round-trip against the configured server). |
| `marvis hooks status` | Reports which governance hooks are present in settings + on disk. |

**Golden rule (repeat):** the agent NEVER hand-writes `settings.json` / `.mcp.json` /
hook files. Always go through the merge primitives above.

---

## 7. Post-install verification

**[DETERMINISTICO: invoke command]** Run these checks and verify every result:

1. `marvis doctor` → must exit 0. If it exits non-zero, **STOP and report the
   failing check and fix text. Do not proceed.**
2. `marvis status` → must be **green**. If it is not green, **STOP and report. Do not
   proceed.**
3. `marvis hooks status` → must show the governance hooks present (in settings + on disk).
4. `marvis mcp status` → must report **connected** (registered AND responds with a tool
   count > 0). If it says "registered but not responding", the interpreter path in
   `.mcp.json` is wrong for this environment — re-run `marvis mcp register` (it writes
   the resolved interpreter) and check again.
5. **Confirm governance is live:** attempt a clearly forbidden action (e.g. a direct
   write to a protected path / a push without a task). It must be **blocked** by a hook.
   If it is not blocked, governance is not active — **STOP and report**.

---

## 8. Telemetry

MarvisX sends **anonymous, aggregated** usage events by default (which CLI commands run,
install funnel, KG scale counts, OS + Python version). It sends **no project content, no
file paths, no PII**. The OSS build runs without our server; telemetry is the only
phone-home, and it is anonymous by construction.

**To turn it off:** set `MARVIS_TELEMETRY=0` in the environment.

Be transparent with the user about this. Because it is opt-out (not opt-in), do not ask
permission — just inform them it is on and tell them the off switch. Full detail ships
in a later slice.

---

## 9. What slice 1 does NOT do

Manage expectations. This install does NOT:

- **Reconcile / upgrade** an existing MarvisX install (the primitives no-op safely, but
  there is no migration).
- **Import deep history** (git log → KG, retroactive handoffs/learnings). New users have
  none; deep history is a later slice.
- **Install the full Codex integration.**
- **Ship a TUI.**

If the user asks for any of these, say they are out of scope for slice 1 and stop.

---

## 10. Troubleshooting

Use this table exactly. Apply one recovery, re-run the failing command, then
continue only if it passes.

| Failure | Recovery |
|---|---|
| `uv: command not found` after installer | **[DETERMINISTICO: invoke command]** `export PATH="$HOME/.local/bin:$PATH" && uv --version`. If it still fails, reopen the shell once and retry. |
| `marvis: command not found` | **[DETERMINISTICO: invoke command]** `export PATH="$HOME/.local/bin:$PATH" && command -v marvis && marvis --version`. If still missing, run `uv tool install marvisx-cli` again. |
| `marvis --version` resolves an old install | **[DETERMINISTICO: invoke command]** `uv tool install --force marvisx-cli && hash -r && marvis --version`. |
| Python too old | **[DETERMINISTICO: invoke command]** stop. Install Python 3.10+ first; do not continue with Python 3.9 or older. |
| `externally-managed-environment` from `pip` | **[DETERMINISTICO: invoke command]** use `uv tool install marvisx-cli`. Do not pass `--break-system-packages`. |
| Native dependency build failure on macOS | **[DETERMINISTICO: invoke command]** `xcode-select --install` if Command Line Tools are missing, then retry `uv tool install marvisx-cli`. |
| Native dependency build failure on Linux | **[DETERMINISTICO: invoke command]** install the system package named in the compiler error, then retry. Common packages: `build-essential`, `pkg-config`, `libgit2-dev`, `libmagic1`, `sqlite3`. |
| `marvis doctor` data-file check fails | **[DETERMINISTICO: invoke command]** `uv tool install --force marvisx-cli`, then `marvis doctor`. This means the wheel/sdist is incomplete. |
| `marvis doctor` says CLI is not on PATH | **[DETERMINISTICO: invoke command]** add the printed tool-bin path to the active shell config, reload once, then run `marvis doctor` again. |
| `settings.json` not found | **[DETERMINISTICO: invoke command]** `marvis hooks install`. It creates and merges the file; do not create it by hand. |
| MCP registered but not responding | **[DETERMINISTICO: invoke command]** `marvis mcp register && marvis mcp status`. The register command rewrites the interpreter path to the current `sys.executable`. |
| Anything red in `marvis status` | **[DETERMINISTICO: invoke command]** stop and report the exact output. Do not patch config files manually. |

Gatekeeper/quarantine note: CI does not exercise macOS Gatekeeper, and this
slice ships a pure-Python CLI through `uv`/`pip`, not a downloaded `.app`, `.pkg`,
or PyInstaller binary. Do not add quarantine workarounds unless a future binary
installer exists.

---

## Self-verify checklist (run this last)

Run each item. Every line must be true before you report success.

- [ ] `python3 --version` ≥ 3.10 and `marvis --version` succeeds.
- [ ] `marvis doctor` exits 0.
- [ ] `marvis status` is **green**.
- [ ] `marvis hooks status` shows the governance hooks present (settings + on disk).
- [ ] `marvis mcp status` reports **connected** (registered AND responds, tool count > 0).
- [ ] A forbidden action was attempted and **blocked** by a hook.
- [ ] The user's pre-existing `settings.json` / `.mcp.json` / `CLAUDE.md` entries are all
      still present (you merged, never clobbered).
- [ ] The confirmed projects appear (`marvis status` / project list).

If any box is unchecked: **STOP, report the failing item, and do not claim the install
succeeded.**
