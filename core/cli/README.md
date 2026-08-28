# marvis init

Interactive bootstrap wizard for the local MarvisX open-core runtime. Five prompts (license,
storage, BYOK, first project, recap) reusing the shared `core/wizard/`
state machine so the CLI and the Console `/welcome` route produce
byte-identical settings when given the same answers.

## Install

```bash
pip install marvisx-cli
marvis init
```

Pre-install (clone and run) works too:

```bash
python -m core.cli.marvis_init init
```

## Quick start (interactive)

```bash
marvis init
```

You will be asked:

1. **License** — accept BSL 1.1 (`y`/`n`).
2. **Storage** — projects root path, database backend (`sqlite` or
   `postgres`), and either the SQLite file path or the Postgres DSN.
3. **LLM provider (BYOK)** — `anthropic`, `openai`, `mac_gateway`,
   `bedrock`, or `skip`. API key prompt is hidden input. Mac gateway
   asks for the base URL.
4. **First project** — name, slug, and type (`code`, `work`, `system`).
5. **Recap** — review choices, confirm.

On confirmation the wizard writes:

- `~/.marvis/settings.yaml` — workspace + storage + llm choice (no
  secrets), chmod 600.
- `~/.marvis/master.key` + `~/.marvis/byok.vault` — Fernet-encrypted
  API key store (only if a provider was chosen).
- `<projects_root>/<slug>/project.yaml` — first project seed.

## Non-interactive / CI

```bash
marvis init \
  --accept-bsl \
  --no-interactive \
  --projects-root /var/lib/marvis/projects \
  --db-backend sqlite \
  --db-path /var/lib/marvis/console.db \
  --llm-provider anthropic \
  --llm-api-key "$ANTHROPIC_API_KEY" \
  --project-name "Local Workspace" \
  --project-slug local-workspace \
  --project-type code
```

YAML preset (preferred for repeatable provisioning):

```bash
marvis init --no-interactive --config init.yaml
```

`init.yaml`:

```yaml
welcome:
  bsl_accepted: true
storage:
  projects_root: /var/lib/marvis/projects
  db_backend: sqlite
  db_path: /var/lib/marvis/console.db
llm_provider:
  provider: anthropic
  api_key: sk-ant-...
first_project:
  name: Local Workspace
  slug: local-workspace
  type: code
```

## Dry run

```bash
marvis init --dry-run --accept-bsl --no-interactive ...
```

Prints the planned filesystem writes and the rendered `settings.yaml`
without touching the disk. Exit 0.

## Exit codes

| Code | Meaning |
| --- | --- |
| 0 | Success (or dry-run) |
| 1 | Generic runtime error |
| 2 | User abort at recap confirmation |
| 3 | Validation failure (field-level errors printed) |

## Tests

```bash
python -m pytest tests/test_cli_marvis_init.py
```

Ten cases cover dry-run, non-interactive flags, YAML preset, BYOK vault
write, invalid slug rejection, Postgres backend, interactive prompts via
`CliRunner(input=...)`, BSL gate, and `show-state` JSON dump.

## Relation to `core/scripts/marvis-init.sh`

`marvis-init.sh` (bash) is a deploy bootstrap: copies
`deploy/_template/`, renders `.env` with secrets, runs
`setup-server.sh`, and brings up Docker Compose. It targets server
administrators provisioning a host.

`marvis init` (this Python CLI) is an onboarding wizard: writes
`settings.yaml` + BYOK vault + first project seed in the user's home.
It targets people running the source-available MarvisX runtime locally.

The two flows are complementary and will converge in a later phase. Until
then both ship side-by-side.
