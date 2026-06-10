# Security Policy

## Reporting a vulnerability

Please do NOT open public issues for security vulnerabilities.

Report privately using GitHub's "Report a vulnerability" flow (this
repository's **Security** tab -> **Advisories** -> **Report a vulnerability**).
<!-- TODO(owner): add a dedicated security contact email (e.g.
security@justaskmarvis.com) once the inbox exists, and list it here as an
alternative channel. -->

Please include:

- a description of the issue and its impact,
- steps to reproduce,
- affected version(s) and your environment (the output of `marvis doctor --json`
  is helpful and contains no secrets).

We aim to acknowledge reports within 5 business days and to agree on a
remediation timeline after triage.

## Supported versions

MarvisX is pre-1.0 software. Security fixes are applied to the latest released
version only. Pin to a released version and upgrade promptly when a security
advisory is published.

## Security posture (self-hosted)

MarvisX is self-hosted: you run your own instance and your data stays on your
machine. The points below describe the honest, current posture so you can
configure it for your environment rather than assume guarantees that do not
exist.

### Visibility model

Each install is an **isolated instance**. There is no central MarvisX server that
sees your data: the cloud component stores only **aggregate counts** (anonymous
usage telemetry) and never your projects, files, or secrets.

Within a single instance, `MARVIS_AGENT_VISIBILITY_BYPASS` defaults to `true`: an
agent can see all projects of that instance. This is correct for the common case
(one person, or one trusted team, running their own box).

If a **team self-hosts a single shared instance** and wants per-project
membership boundaries (one member should not see another project), set
`MARVIS_AGENT_VISIBILITY_BYPASS=false`. The default is convenience for the
single-tenant case, not an enforced multi-tenant security boundary.

### `check_safety` is a guardrail, not a sandbox

The `check_safety` preflight for `file_write` is **permissive by design**. The
worktree rule (code edits happen in isolated worktrees / feature branches, never
on `main`) is what governs code changes; `check_safety` is a fast advisory check,
**not a sandbox**. Do not rely on it to contain a malicious or compromised agent.
Run agents with OS-level permissions you are comfortable granting.

### Local data is local

`console.db` (including ingested document `extracted_text`) is stored **locally
in cleartext** on the machine running the instance. PII detection during ingest
is **descriptive, not blocking**: it annotates, it does not refuse or redact.

This data is **local only** and is never synced to the cloud — only aggregate
counts leave the machine. Protect `console.db` with normal filesystem permissions
and full-disk encryption if the host warrants it.

### BYOK vault — `master.key` at rest

Your bring-your-own-key (BYOK) provider API keys live in `~/.marvis/byok.vault`,
encrypted with a Fernet "master key". That master key is itself protected at rest
**when a passphrase source is available**:

- Set `MARVIS_MASTER_PASSPHRASE` (or store the passphrase in the OS keyring, an
  optional dependency) and the master key is wrapped with a key-encryption key
  derived from the passphrase via **scrypt** (a memory-hard KDF). The wrapped key
  is stored as `master.key.enc` next to a random `master.key.salt`; the raw key
  never touches disk.
- **Backward compatibility / no lockout:** an existing cleartext `master.key`
  keeps working. If a passphrase becomes available it is transparently migrated to
  the encrypted form (and the cleartext file removed). If no passphrase source
  exists (e.g. a headless server with no keyring), the cleartext `master.key`
  continues to work and a one-time warning is logged. The vault is never locked,
  and a wrong passphrase produces a clear error rather than corruption.

To protect the master key on a headless deployment, set `MARVIS_MASTER_PASSPHRASE`
in the environment; the next vault access migrates the key to the encrypted form
automatically.
