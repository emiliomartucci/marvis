# Compound playbook

You just finished something — a fix, a feature, a hard debugging session. Capture
what was actually built and learned so the next person (or the next you) does not
re-walk the same path. The point of compounding is that each solved problem makes
the next one cheaper. You (the host agent) run the steps below; this playbook only
directs you.

## 1. CONSULT — ground yourself in the brain first

{brain_context}

Before writing the capture, pull the relevant memory so you record what is new
rather than duplicating what is already known:

- `mcp__marvis__search("{what}")` — related solutions, plans, and tasks across
  every project, ranked by meaning. If a near-identical solution already exists,
  you may be extending it rather than writing a fresh one — link to it.
- `mcp__marvis__check_learnings("{what}")` — existing prevention rules in this
  area. If your lesson restates one that already exists, reinforce that one
  instead of creating a near-duplicate.
- If a project was named, `mcp__marvis__session_brief("<project>")` for its open
  tasks and latest handoff, so the capture lands in the right context.

Read the top hits in full. If nothing relevant comes back, say so and move on —
do not invent links.

## 2. DO — distill what was built and learned

Write the durable account, not a play-by-play. Cover, tightly:

- **The problem** — what was actually wrong, the symptom, and why it mattered.
  The version a future reader will search for when they hit the same symptom.
- **The solution** — what you did and why this approach over the alternatives.
  Link the code or the plan it came from; do not paraphrase away its context.
- **The gotchas** — the non-obvious traps: what looked right but wasn't, the
  surprising interaction, the thing that cost you an hour. This is the highest-
  value part — be concrete.
- **What you'd do differently** — the one or two changes that would have made it
  faster or safer, stated plainly.

**Capability-tiered distillation.** If your host can spawn parallel subagents, fan
out — one to reconstruct the diff/commit history, one to pull the related prior
work and learnings from step 1 — then merge. If your host cannot spawn subagents
(Codex, Cursor, Cline, or any single-thread agent), do the same sequentially.
Skip an angle when you already have the answer in context; do not pad.

Keep it honest and specific. A vague capture is worse than none — it pollutes
search without teaching anything.

## 3. SAVE — write it back to the brain (TWO things)

Compounding needs both a readable account AND a reusable rule. Do both:

1. **The solution doc** — call `mcp__marvis__save_compound(project, title, body)`
   with the full distillation as `body`. Do NOT hand-write the file: the tool
   places it in the project's `docs/solutions/` folder with the correct dated,
   kebab-cased name AND embeds it so the next `search` finds it by meaning. Use a
   clear, searchable title (3-5 descriptive words, e.g. `cart total race fix` or
   `onnx tokenizer cold start`), not "compound: thing".

2. **The durable rule** — call `mcp__marvis__create_learning(...)` for the ONE
   tight, reusable prevention rule this experience yields: a crisp title, the
   category and severity, what went wrong, and the single prevention rule a future
   agent should follow to avoid it. One rule, not a list — if you have several,
   keep the one with the widest reach. `create_learning` embeds on write, so the
   lesson is immediately retrievable by the next `check_learnings`; that is what
   makes the next risky action safer.

Both calls return references — report the solution path (and that the learning was
created) to the user.
