# Marvis repository contract

This repository is the public source surface for the Marvis open-core product.

- Git/GitHub and verified worktrees own source, commits, tests, and PRs.
- When work belongs to a configured Marvis project, load its session brief through
  the host-provided connector. Hosted mirrors are discovery metadata, not source.
- Never substitute a local runtime, database, CLI, or alternate connector for an
  unavailable hosted project. Continue independent authorized work.
- Follow the README, repository tests, security policy, hooks, and release workflow.
- Preserve local changes and unpublished commits. Use an isolated worktree for
  independent work; report existing changes and what should be included separately.
- Never commit credentials, tenant data, generated local state, or customer records.
- Run checks relevant to the diff. Report source, merge, release, and observed
  runtime separately; documentation changes do not require a runtime deployment.
