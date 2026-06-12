# The Marvis Way

> **If you are an AI agent, this is your operating manual — read it before you
> touch the user's repository.** It tells you exactly how to organize their
> work, where to write what, and how to leave the workspace better than you
> found it so the next agent (or you, tomorrow) picks up without losing context.
> Everything here is concrete and you can act on it immediately.

Marvis is a **mold** for how an agent-run workspace keeps its memory. It does
not replace your agent — Claude Code, Codex, Cursor, whatever you use — it gives
that agent a place to put what it learns, a shape for your projects, and a way
to find prior decisions across them.

This guide is the source of truth for how that shape works. Read it, then apply
it to your own repository: an agent can use these conventions to organize a
messy folder, write the missing context, and let Marvis index the result. You
do not have to adopt everything at once — start with one project and a
`context.md`, and grow into the rest.

**Installing on a locked-down machine.** If `uv tool install` is blocked by an
organization's application-control policy (Windows WDAC / AppLocker / Managed
Installer — common on managed corporate machines), do not bypass it. Install
Marvis inside WSL2 (recommended) or Docker, or ask IT to whitelist `uv`; the
Linux runtime is identical.

Sections (jump with `marvis guide --section <name>`): concepts, naming,
directories, dependencies, tags, frontmatter, brain, local console gui,
lifecycle, adopt, onboarding completion.

## Concepts

Three nested units organize everything:

- **Program** — a long-running line of work or a team area. Optional grouping
  over projects (e.g. "platform", "growth"). Programs give cross-project views.
- **Project** — the main unit. One folder, one `project.yaml`. A project has a
  `type`: `code` (has a git repo and ships PRs), `work` (docs/research, no git),
  or `system` (infrastructure). Everything below — context, memory, docs — hangs
  off a project.
- **Task** — a single tracked piece of work inside a project. Tasks survive
  across agent sessions, so an agent that picks up tomorrow sees what was open.
  Tasks carry an intent score (impact / confidence / ease) and a delegation
  (agent, hybrid, human).

The rule of thumb: **one thing in progress at a time per worker**, and every
implementation change starts by creating a task — so the work is tracked before
the first edit, not reconstructed afterwards.

## Naming

- **Slugs** are kebab-case, lowercase, stable: `growth-site`, `q3-research`.
  A slug never changes once other things reference it.
- **Canonical IDs** in the Knowledge Graph follow `{prefix}:{kind}:{slug}`,
  for example `task:artifact:<uuid>`, `handoff:artifact:2026-06-04-onboarding`,
  `py:function:core.api.db.get_db`. The prefix says what kind of thing it is;
  the kind says whether it is a file, function, module, or artifact.
- **Files** are dated when they are events (`handoff-2026-06-04-<topic>.md`) and
  named by subject when they are references (`context.md`, `project.yaml`).

## Directories

A project is a folder. The conventional layout:

```
<project-slug>/
  project.yaml          # name, slug, type, lifecycle  (the only required file)
  context.md            # the living state of the project: goal, status, constraints
  memory/
    handoff-YYYY-MM-DD-<topic>.md   # what happened in a session, for the next one
  docs/
    brainstorms/        # exploring WHAT to build
    plans/              # deciding HOW to build it
    solutions/          # what was built and why (post-hoc, durable)
  input/                # raw material you bring in (transcripts, exports, briefs)
  output/               # finished artifacts you hand out
```

`code` projects keep their actual source wherever it already lives (a `repo_path`
in `project.yaml` points at it); the project folder holds the *memory* around the
code, not necessarily the code itself.

## Dependencies

Marvis links artifacts in a **Knowledge Graph** so an agent can ask "who calls
this", "what breaks if I change it", and "why does this exist" instead of
grepping blind. The edges are deterministic — derived from the code and the
documents, not guessed by a model. They fall into categories:

- **Code**: `calls`, `imports`, `defines` — extracted from the source tree.
- **Work chain**: `produces`, `contains` — a task produces a PR; a project
  contains its docs.
- **Knowledge chain**: `describes`, `documents`, `cites`, `applies_to` — a
  handoff describes a task; a learning applies to a module.
- **Cross-project**: `depends_on`, `mentions`, `refers_to`, `shares_tag`,
  `similar_to` — how one project's work relates to another's.
- **Bridge**: `resolves_to` — links a referenced symbol to its canonical file.

You never write edges by hand. They are populated from what you commit and the
frontmatter you fill in. The authoritative, live set of edge types for an
install is what the graph reports — treat the list above as the shape, not a
fixed count.

## Tags

Tags are free-form, lowercase, kebab-case labels on tasks, handoffs, and
learnings. They power search and the `shares_tag` cross-project edge. Keep them
few and meaningful: a topic (`auth`, `billing`), a surface (`frontend`, `api`),
or a workstream (`q3-launch`). Tags are not a taxonomy you design up front; they
accrete, and search does the rest.

## Frontmatter

Every durable markdown artifact starts with a YAML frontmatter block. It is how
Marvis indexes the file and how an agent reads its metadata without parsing
prose. The body below the block is **only content** — no "this document is for
X" preamble; audience and status live in the frontmatter.

A handoff:

```yaml
---
date: 2026-06-04
project: growth-site
tags: [onboarding, launch]
---
```

A plan or brainstorm adds `title`, `type`, `status`. A solution records what was
built. The minimum that makes a file first-class is a `date` (for events) or a
`title` + `project` (for references). Missing frontmatter does not break
anything — the file is just weaker signal until you add it.

## Brain

The Brain is a reflection layer that runs on a schedule (or on demand with
`marvis brain run`). It does not write your code; it watches the work and keeps
the memory honest. It runs in layers:

1. **Substrate** — collects what changed since last time.
2. **Digest & journal** — a short narrative of what happened, per project.
3. **Drift** — compares what was produced against the stated intent and flags
   where they diverge before it compounds.
4. **Memory operations** — proposes consolidating duplicates, superseding stale
   notes, hardening provenance. Proposals, not silent edits.
5. **Findings** — conclusions worth your attention, queued for you to approve.

The free mode never uses an LLM key — it is local upkeep. The full mode adds an
LLM pass for narrative polish and citations when you have configured a key.

## Local Console GUI

`marvis console` is the local browser GUI. It verifies or starts the local API on
`127.0.0.1:8100`, then opens `http://127.0.0.1:8100/ui/` in your browser. Use
`marvis console --no-open` when you want the URL printed without opening a tab.
If port 8100 is already owned by another process, Marvis refuses to open the
page because that is not the Marvis API.

On first run, the Console walks you through a 5-step onboarding wizard. It can
seed Casa Lorenzi demo data so you can inspect the product before importing your
own work. Demo items are badged and removable from the tour's final card, or by
calling `DELETE /api/v1/onboarding/demo` on the local API. The wizard also starts
a spotlight tour of the interface; rerun it any time from the help icon.

The wizard authors `setup.md` in your Marvis vault (`~/.marvis/setup.md` by
default). This file is the explicit human contract for **Identità**,
**Sorgenti**, **Ritmo**, and **Fonti del brain**: who you are, what sources
matter, when the brain should reflect, and which inputs it can use. Projects and
programs are not written into `setup.md`; Marvis derives them from `marvis init`
and imported project metadata.

The main surfaces are:

- **Diario** — the default view, showing the nightly brain journal and recent
  context.
- **Todos** — one queue for capture, reminders, approvals, and proposed actions.
- **Task** — tracked work with status, owner, project, tags, and review state.
- **Progetti** — project records, context, and imported work roots.
- **Universe** — the Knowledge Graph view across projects, files, tasks, and
  decisions.

For a persistent local GUI, use `marvis autostart enable|disable|status`.
Autostart creates the OS login service for the local API: `launchd` on macOS,
`systemd --user` on Linux, and a Windows Scheduled Task on Windows. Enable it
when you want a menu-bar icon, dock shortcut, or bookmark to open a live page
instead of a dead local URL.

The Console status bar shows the installed version and an update hint when a
newer build is expected. Upgrade with:

```bash
pip install -U marvisx-cli
```

## Lifecycle

Work moves through a fixed set of states so nothing is silently dropped:

```
pending  →  approved  →  in_progress  →  review  →  completed
                                                     (or rejected / failed)
```

- **pending** — created, not yet greenlit. A human approves it.
- **approved** — greenlit, ready to start.
- **in_progress** — being worked. Exactly one per worker at a time.
- **review** — a `code`/`system` task opens a pull request and waits for a human
  to merge it. `completed` is reached by the merge, not set by hand.
- **completed** — done and verified.

For `work` projects there is no PR — the task completes when its document
exists. For quick diagnostics, a task can complete with no artifact at all.

## Adopt

To bring an existing, messy folder into this shape, point your agent at this
guide and ask it to work **non-destructively** — map first, then propose:

1. **Read** the folder and group files by the project they belong to.
2. **Propose** a structure (the layout above), one project at a time, and wait
   for your OK before moving anything. Nothing is deleted or overwritten without
   confirmation.
3. **Create** the missing `project.yaml` and a `context.md` from the templates,
   and add frontmatter to the documents that lack it.
4. **Index** — let Marvis read the resulting project folders. The Knowledge
   Graph and search build themselves from there.

Marvis is the mold; your agent does the shaping. You stay in control of every
move.

## Onboarding completion

`marvis doctor` reports whether your install is **complete**, as binary done/not-done
states. The "100%" is only the **required** ones — the **recommended** ones unlock
more but never hold you back if you don't want them. Each open state prints its fix.

<!-- marvis:onboarding-states:start -->
**Required** (without these Marvis does not work):

- **CLI installed and on PATH** — the `marvis` command resolves in your shell. Fix: `reinstall: uv tool install marvisx-cli`.
- **Config present and valid** — `marvis init` created a readable `settings.yaml` in the vault. Fix: `marvis init`.
- **MCP server registered in Claude Code** — your agent can call the Marvis tools from Claude Code. Fix: `marvis mcp`.

**Recommended** (each unlocks more):

- **Governance hooks installed** — the repo has the Marvis safety and quality hooks installed. Fix: `marvis hooks install`.
- **At least one project imported** — Marvis has at least one project folder to work on. Fix: `marvis project import <path>`.
- **Code indexed (projects with code)** — the Knowledge Graph has indexed code for project search and impact checks. Fix: `marvis project index <slug>`.
- **Brain enabled and scheduled** — daily reflection can write the brain journal without manual runs. Fix: `marvis brain enable`.
- **LLM configured (brain not mute)** — the brain has a writing model for narrative summaries and citations. Fix: `set BRAIN_LLM_GATEWAY_API_KEY (or use the local-model / claude -p path)`.
- **Local Console GUI packaged** — the installed wheel includes the browser GUI served by the local API. Fix: `pip install -U marvisx-cli, then run: marvis console`.
- **Console autostart enabled** — the local API starts at login so the Console icon does not open a dead page. Fix: `marvis autostart enable`.
- **setup.md authored contract present** — the vault has `setup.md` with Identità, Sorgenti, Ritmo, and Fonti del brain. Fix: `marvis console, then complete the 5-step onboarding wizard`.
- **Work sources and exclusions configured** — `setup.md` records the folders to index and the folders to exclude. Fix: `marvis console, then complete the wizard's Sources step`.
- **Casa Lorenzi demo data seeded** — the optional badged demo data is present and can be removed later. Fix: `marvis console, then seed the Casa Lorenzi demo in the wizard`.
<!-- marvis:onboarding-states:end -->

Run `marvis doctor` any time to see which of these are done and the command to
close each gap.
