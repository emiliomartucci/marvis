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

Sections (jump with `marvis guide --section <name>`): concepts, naming,
directories, dependencies, tags, frontmatter, brain, lifecycle, adopt.

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
