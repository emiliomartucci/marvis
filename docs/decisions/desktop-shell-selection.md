---
title: Desktop shell selection
status: open
date: 2026-07-29
supersedes: none
related:
  - contracts/desktop-host.yaml
  - apps/desktop-ui/surfaces.yaml
  - marvisx:docs/plans/2026-07-24-separate-marvis-product-surfaces-plan.md
---

# Desktop shell selection

## Status

**Open.** No technology has been chosen. The surface-separation plan assigns
the desktop GUI to `marvis` and deliberately does not select its shell (KTD4);
an earlier premature commitment was removed from that plan for this reason.

## What is already settled

These do not need to be reopened when the shell is chosen:

- The local product owns its GUI source (`apps/desktop-ui`) and its perimeter
  is enforced in CI.
- A shell reaches the product over loopback and drives the documented
  capabilities; it does not reimplement them (`contracts/desktop-host.yaml`).
- Permissions are owned by the local runtime, so CLI, MCP and any shell get
  the same answer.
- The browser launcher remains the compatibility path.

## What the deciding ADR must answer

1. **Runtime footprint** — installed size, memory at idle, cold start, measured
   on the three supported platforms rather than quoted from documentation.
2. **Threat model** — what the shell can reach that a browser cannot, how the
   loopback endpoint is protected from other local processes, and what an
   installed shell exposes if the machine is shared.
3. **Update path** — how a shell updates itself, what happens when shell and
   runtime versions disagree, and how a user recovers from a failed update.
4. **Signing and notarization** — per platform, including who holds the
   credentials and where they live. Credentials never enter this repository.
5. **Offline behaviour** — what the shell does when the runtime is not
   running, and whether it may start it.
6. **Exit cost** — what has to change to replace the shell later. A candidate
   that can only be replaced by rewriting product code fails this criterion.

## Non-goals for that ADR

Connecting the desktop shell to Cloud organizations, and any change to the
ownership map, the local API surface or the data lifecycle. If a candidate
requires one of those, that is a finding about the candidate.
