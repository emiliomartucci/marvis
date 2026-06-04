# Brainstorm playbook

Explore WHAT to build before anyone decides HOW to build it. Turn a fuzzy topic
into a small set of concrete, comparable options grounded in what the brain
already knows, then save the thinking so the plan that follows starts from it.
You (the host agent) run the steps below; this playbook only directs you.

## 1. CONSULT — ground yourself in the brain first

{brain_context}

Before generating any options, pull the relevant memory so you explore around
what is already known, not over it:

- `mcp__marvis__search("{topic}")` — related brainstorms, plans, tasks, and files
  across every project, ranked by meaning (not just keywords).
- `mcp__marvis__check_learnings("{topic}")` — past post-mortems and prevention
  rules that bear on this topic. Anything that surfaces is a hard constraint that
  fences off whole branches of the option space — respect it.
- If a project was named, `mcp__marvis__session_brief("<project>")` for its open
  tasks, latest handoff, and recent learnings.

Read the top hits in full, not just their titles. Note what already exists and
which decisions are locked, so the brainstorm does not re-open settled questions.
If nothing relevant comes back, say so and move on — do not invent links.

## 2. DO — explore the option space (WHAT, not HOW)

Stay on WHAT to build and WHY, not on implementation mechanics — the HOW belongs
to the plan that comes next. Your job here is to widen, then narrow.

Generate **2-3 concrete, genuinely different approaches** to the topic — not one
idea dressed three ways. For each, state plainly:

- what it actually is, in one or two sentences;
- what you gain (the upside, who it serves, what it unlocks);
- what you pay (the cost, the risk, what it forecloses or complicates).

**Apply YAGNI ruthlessly.** Cut anything speculative — features "we might want
later", abstractions with one caller, options that exist only to look thorough.
The cheapest approach that solves the real problem usually wins; make the burden
of proof fall on the more elaborate options, not the simple one.

**Ask the user one question at a time — but only at a real fork.** When two
approaches genuinely diverge on a decision only the user can make (a
priority, a tradeoff between cost and reach, a scope boundary), stop and ask that
single question before continuing. Do not batch questions, and do not ask about
anything you can resolve yourself by reading the brain or the codebase.

**Capability-tiered exploration.** If your host can spawn parallel subagents, fan
out the research angles at once — one per candidate approach, plus one on the
relevant prior work and learnings from step 1 — then merge what comes back into
the comparison. If your host cannot spawn subagents (Codex, Cursor, Cline, or any
single-thread agent), run the same angles sequentially yourself — same coverage,
one after the other. Skip an angle when local context already answers it; do not
pad.

Close with a clear recommendation: which approach, and the one-line reason. The
recommendation is a proposal for the plan step to act on, not a commitment.

## 3. SAVE — write it back to the brain

Call `mcp__marvis__save_brainstorm(project, title, body)` with the full
exploration as `body` — the approaches, their tradeoffs, the open questions, and
the recommendation. Do NOT hand-write the file: the tool places it in the
project's `docs/brainstorms/` folder with the correct dated, kebab-cased name AND
embeds it so the next `search` (and the `plan` that follows) finds it by meaning
immediately — that is how the loop compounds.

Use a clear, searchable title (3-5 descriptive words, e.g. `notification
delivery options` or `pricing model exploration`), not "brainstorm: thing". The
tool returns `{path, embedded}`; report the path to the user.
