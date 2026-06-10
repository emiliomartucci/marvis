# Plan playbook

Turn a feature, bug, or improvement into a structured, brain-grounded plan, then
save it so the next session can find and build on it. You (the host agent) run
the steps below; this playbook only directs you.

## 1. CONSULT — ground yourself in the brain first

{brain_context}

Before drafting anything, pull the relevant memory so you do not re-decide what
was already decided:

- `mcp__marvis__search("{feature}")` — related plans, brainstorms, tasks, and
  files across every project, ranked by meaning (not just keywords).
- `mcp__marvis__check_learnings("{feature}")` — past post-mortems and prevention
  rules that apply. Treat anything that surfaces here as a hard constraint, not a
  suggestion.
- If a project was named, `mcp__marvis__session_brief("<project>")` for its open
  tasks, latest handoff, and recent learnings.

Read the top hits in full, not just their titles. Write a short "prior work"
note: what already exists, which decisions are locked, and what is genuinely new
here. If nothing relevant comes back, say so and move on — do not invent links.

## 2. DO — research, then structure the plan

Gather context, then shape it into a plan. Match the depth to the work: a small
fix needs a few lines; an architectural change needs phases and a system-wide
impact pass.

**Capability-tiered research.** If your host can spawn parallel subagents, fan
out across these angles at once and merge the results:

- existing patterns and conventions in this codebase that the change must follow;
- the relevant institutional learnings and prior solutions (from step 1);
- external best practices — but only for genuinely risky or unfamiliar territory
  (security, payments, external APIs, data privacy, new technology).

If your host cannot spawn subagents (Codex, Cursor, Cline, or any single-thread
agent), run the same angles sequentially yourself — same coverage, one after the
other. Skip external research when local context is already strong; do not pad.

**Structure the plan** with:

- a one-paragraph overview and the problem statement (why this matters now);
- the proposed solution and, for non-trivial work, the technical approach and
  release-able phases;
- a system-wide impact pass for anything that touches shared state: what fires
  when this runs, how errors propagate, where partial failure could orphan state,
  which other surfaces expose the same path and need the same change;
- measurable acceptance criteria and the quality gates that must be green;
- dependencies, sequencing, and the real risks with their mitigations;
- a sources section that links the prior work from step 1 by path.

Keep decisions traceable: when you carry a conclusion forward from a brainstorm
or a learning, link back to its source instead of paraphrasing away its context.

## 3. SAVE — write it back to the brain

Call `mcp__marvis__save_plan(project, title, body)` with the full plan as
`body`. Do NOT hand-write the file: the tool places it in the project's
`docs/plans/` folder with the correct dated, kebab-cased name AND embeds it so the
next `search` finds it by meaning immediately — that is how the loop compounds.

Use a clear, searchable title in conventional form, e.g. `feat: add user
authentication flow` or `fix: cart total race condition` (3-5 descriptive words,
not "feat: thing"). The tool returns `{path, embedded}`; report the path to the
user.
