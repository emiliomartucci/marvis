"""`marvis init` — interactive bootstrap wizard for OSS users.

Walks the user through 5 prompts (BSL / storage / BYOK / first project /
recap) reusing `core.wizard` shared lib so the CLI and the Console
`/welcome` route produce byte-identical settings when given the same
answers.

Outputs (default ~/.marvis):
- settings.yaml  → workspace + storage + llm choice (no secrets)
- master.key.enc + master.key.salt → Fernet key wrapped with a passphrase-derived
  KEK, written when MARVIS_MASTER_PASSPHRASE (or an OS keyring / TTY prompt) is
  available; otherwise a cleartext master.key (chmod 600) + a one-time warning.
- byok.vault     → encrypted API key store
- <projects_root>/<slug>/project.yaml → first project seed
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import click
import typer
import yaml
from rich.console import Console
from rich.table import Table

from core.wizard import (
    DbBackend,
    FirstProjectPayload,
    LlmProvider,
    LlmProviderPayload,
    ProjectType,
    StoragePayload,
    ValidationError,
    WelcomePayload,
    WizardState,
    advance,
    finalize,
    slugify,
    validate_first_project,
    validate_llm_provider,
    validate_storage,
    validate_welcome,
)
from core.wizard.byok_vault import (
    DEFAULT_VAULT_DIR,
    VAULT_FILENAME,
    ensure_master_key,
    mask_api_key,
    store_provider_key,
)
from core.wizard.defaults import (
    default_db_path,
    default_first_project,
    default_projects_root,
)

DEFAULT_SETTINGS_FILENAME = "settings.yaml"


def _resolve_cli_version() -> str:
    """Version string for ``marvis --version``, read from the installed package
    so it tracks the wheel automatically. It used to be a hardcoded constant
    that silently drifted (stuck at an old release across version bumps)."""
    from importlib.metadata import PackageNotFoundError, version

    try:
        return f"marvis {version('marvisx-cli')}"
    except PackageNotFoundError:  # running from source, not pip-installed
        return "marvis (dev)"


CLI_VERSION = _resolve_cli_version()


def _default_vault_dir() -> Path:
    """Resolve the vault dir honoring ``$MARVIS_VAULT_DIR`` → ``~/.marvis``.

    Mirrors ``core.telemetry.client._marvis_dir()`` so that, when ``--vault-dir``
    is not passed, ``marvis init`` writes to the env-configured vault instead of
    silently polluting the real ``~/.marvis``.
    """
    env_dir = os.environ.get("MARVIS_VAULT_DIR")
    return Path(env_dir).expanduser() if env_dir else DEFAULT_VAULT_DIR
BSL_NOTICE = (
    "MarvisX is distributed under the Business Source License 1.1. "
    "Self-hosting and internal business use are free; a paid license is "
    "required only to offer it to third parties as a hosted or managed "
    "service. Full text: https://justaskmarvis.com/license"
)

app = typer.Typer(
    add_completion=False,
    no_args_is_help=False,
    rich_markup_mode=None,
    help=(
        "marvis-init — interactive bootstrap wizard.\n\n"
        "Run with no arguments for a guided 5-step setup, or pass flags "
        "(see --help) to skip prompts in CI."
    ),
)
console = Console(stderr=False)
err_console = Console(stderr=True)


def _render_errors(errors: list[ValidationError]) -> str:
    return "\n".join(f"  - {e.field}: {e.message}" for e in errors)


def _is_interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def _load_preset(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise typer.BadParameter(f"Config file not found: {path}")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise typer.BadParameter(f"Invalid YAML in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise typer.BadParameter(f"Config root must be a mapping in {path}")
    return data


def _prompt_welcome(
    *, accept_bsl: bool, interactive: bool
) -> WelcomePayload:
    if accept_bsl:
        return WelcomePayload(bsl_accepted=True)
    if not interactive:
        raise typer.BadParameter(
            "BSL not accepted. Pass --accept-bsl or run interactively."
        )
    console.print()
    console.rule("[bold]Step 1/5 — License")
    console.print(BSL_NOTICE)
    accepted = typer.confirm(
        "Do you accept the Business Source License 1.1?", default=False
    )
    return WelcomePayload(bsl_accepted=accepted)


def _prompt_storage(
    *,
    projects_root: str | None,
    db_backend: str | None,
    db_path: str | None,
    postgres_dsn: str | None,
    interactive: bool,
) -> StoragePayload:
    backend_value = db_backend or DbBackend.sqlite.value
    if interactive:
        console.print()
        console.rule("[bold]Step 2/5 — Storage")
        projects_root = typer.prompt(
            "Projects folder (absolute path)",
            default=projects_root or default_projects_root(),
        )
        backend_value = typer.prompt(
            "Database backend",
            default=backend_value,
            type=click.Choice([b.value for b in DbBackend]),
        )
        if backend_value == DbBackend.sqlite.value:
            db_path = typer.prompt(
                "SQLite file path",
                default=db_path or default_db_path(),
            )
            postgres_dsn = None
        else:
            postgres_dsn = typer.prompt(
                "Postgres DSN (postgresql://user:pass@host:port/db)",
                default=postgres_dsn or "",
            )
            db_path = None
    else:
        projects_root = projects_root or default_projects_root()
        if backend_value == DbBackend.sqlite.value:
            db_path = db_path or default_db_path()
            postgres_dsn = None
        else:
            db_path = None

    return StoragePayload(
        projects_root=projects_root,
        db_backend=DbBackend(backend_value),
        db_path=db_path,
        postgres_dsn=postgres_dsn,
    )


def _prompt_llm(
    *,
    provider: str | None,
    api_key: str | None,
    base_url: str | None,
    interactive: bool,
) -> LlmProviderPayload:
    if interactive:
        console.print()
        console.rule("[bold]Step 3/5 — LLM provider (BYOK)")
        valid = [p.value for p in LlmProvider] + ["skip"]
        choice = typer.prompt(
            "LLM provider",
            default=provider or "skip",
            type=click.Choice(valid),
        )
        if choice == "skip":
            return LlmProviderPayload()
        provider_enum = LlmProvider(choice)
        api_key = typer.prompt(
            "API key (hidden input)", hide_input=True, default=api_key or ""
        )
        if provider_enum == LlmProvider.mac_gateway:
            base_url = typer.prompt(
                "Mac Gateway base URL", default=base_url or ""
            )
        return LlmProviderPayload(
            provider=provider_enum,
            api_key=api_key or None,
            base_url=base_url or None,
        )

    if not provider or provider == "skip":
        return LlmProviderPayload()
    return LlmProviderPayload(
        provider=LlmProvider(provider),
        api_key=api_key or None,
        base_url=base_url or None,
    )


def _prompt_first_project(
    *,
    name: str | None,
    slug: str | None,
    project_type: str | None,
    interactive: bool,
) -> FirstProjectPayload:
    defaults = default_first_project()
    type_value = project_type or defaults.type.value

    if interactive:
        console.print()
        console.rule("[bold]Step 4/5 — First project")
        name = typer.prompt("Project name", default=name or defaults.name)
        slug = typer.prompt(
            "Slug",
            default=slug or slugify(name) or defaults.slug,
        )
        type_value = typer.prompt(
            "Project type",
            default=type_value,
            type=click.Choice([t.value for t in ProjectType]),
        )
    else:
        name = name or defaults.name
        slug = slug or slugify(name) or defaults.slug

    return FirstProjectPayload(
        name=name,
        slug=slug,
        type=ProjectType(type_value),
    )


def _validate_step(
    payload: WelcomePayload
    | StoragePayload
    | LlmProviderPayload
    | FirstProjectPayload,
    *,
    allow_llm_empty: bool = True,
) -> None:
    if isinstance(payload, WelcomePayload):
        errors = validate_welcome(payload)
    elif isinstance(payload, StoragePayload):
        errors = validate_storage(payload)
    elif isinstance(payload, LlmProviderPayload):
        errors = validate_llm_provider(payload, allow_empty=allow_llm_empty)
    elif isinstance(payload, FirstProjectPayload):
        errors = validate_first_project(payload)
    else:  # pragma: no cover - defensive
        raise TypeError(f"Unknown payload type: {type(payload).__name__}")

    if errors:
        err_console.print("[red]Validation failed:[/red]")
        err_console.print(_render_errors(errors))
        raise typer.Exit(code=3)


def _print_recap(state: WizardState) -> None:
    table = Table(
        title="Step 5/5 — Recap", show_header=True, header_style="bold cyan"
    )
    table.add_column("Field")
    table.add_column("Value")

    table.add_row("BSL accepted", str(state.welcome.bsl_accepted))
    if state.storage:
        table.add_row("projects_root", state.storage.projects_root)
        table.add_row("db_backend", state.storage.db_backend.value)
        if state.storage.db_backend == DbBackend.sqlite:
            table.add_row("db_path", state.storage.db_path or "")
        else:
            table.add_row(
                "postgres_dsn",
                state.storage.postgres_dsn[:30] + "..."
                if state.storage.postgres_dsn
                and len(state.storage.postgres_dsn) > 30
                else state.storage.postgres_dsn or "",
            )
    if state.llm_provider and state.llm_provider.provider:
        table.add_row("llm_provider", state.llm_provider.provider.value)
        table.add_row("llm_api_key", mask_api_key(state.llm_provider.api_key))
        if state.llm_provider.base_url:
            table.add_row("llm_base_url", state.llm_provider.base_url)
    else:
        table.add_row("llm_provider", "skipped")
    if state.first_project:
        table.add_row("project_name", state.first_project.name)
        table.add_row("project_slug", state.first_project.slug)
        table.add_row("project_type", state.first_project.type.value)
    console.print()
    console.print(table)


def _build_settings_dict(state: WizardState) -> dict[str, Any]:
    storage_dict: dict[str, Any] = {}
    if state.storage:
        storage_dict = {
            "projects_root": state.storage.projects_root,
            "db_backend": state.storage.db_backend.value,
        }
        if state.storage.db_path:
            storage_dict["db_path"] = state.storage.db_path
        if state.storage.postgres_dsn:
            storage_dict["postgres_dsn"] = state.storage.postgres_dsn

    llm_dict: dict[str, Any] = {"provider": None}
    if state.llm_provider and state.llm_provider.provider:
        llm_dict = {
            "provider": state.llm_provider.provider.value,
        }
        if state.llm_provider.base_url:
            llm_dict["base_url"] = state.llm_provider.base_url

    project_dict: dict[str, Any] = {}
    if state.first_project:
        project_dict = {
            "name": state.first_project.name,
            "slug": state.first_project.slug,
            "type": state.first_project.type.value,
        }

    return {
        "version": state.version,
        "bsl_accepted": state.welcome.bsl_accepted,
        "completed_at": state.completed_at.isoformat()
        if state.completed_at
        else None,
        "storage": storage_dict,
        "llm": llm_dict,
        "first_project": project_dict,
    }


def _write_settings(path: Path, payload: dict[str, Any]) -> None:
    from core.platform import secure_path

    path.parent.mkdir(parents=True, exist_ok=True)
    secure_path(path.parent, mode=0o700)
    path.write_text(
        yaml.safe_dump(payload, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    secure_path(path, mode=0o600)


def _seed_project(state: WizardState) -> Path | None:
    if not state.storage or not state.first_project:
        return None
    project_dir = (
        Path(state.storage.projects_root).expanduser()
        / state.first_project.slug
    )
    project_dir.mkdir(parents=True, exist_ok=True)
    project_yaml = project_dir / "project.yaml"
    if project_yaml.exists():
        return project_yaml
    project_yaml.write_text(
        yaml.safe_dump(
            {
                "name": state.first_project.name,
                "slug": state.first_project.slug,
                "type": state.first_project.type.value,
                "lifecycle": "active",
            },
            sort_keys=False,
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    return project_yaml


def _bootstrap_schema(state: WizardState) -> str | None:
    """Create + migrate the local SQLite DB so `marvis status` is green after init.

    The install path (this wizard) is the canonical first step; nothing else runs
    the schema for an OSS single-user runtime (the FastAPI lifespan + the API
    container's init.sh do it for the managed deployment, but neither runs here).
    Without this, a clean machine has ``db_ok:false`` until something migrates.

    Migration 016 seeds the admin user and REQUIRES a password
    (``PIR_ADMIN_PASSWORD_HASH`` / ``PIR_PASSWORD``). In OSS single-user there is
    NO login — the runtime acts as the local operator (``LOCAL_CTX``), no JWT is
    ever issued, so the seed password is never used to authenticate. We therefore
    generate a RANDOM throwaway password purely to satisfy the seed (same idea as
    the test suite's throwaway hash) and never print, store, or expose it.

    SQLite only: a Postgres backend manages its own schema lifecycle, so we skip.
    Best-effort: a migration failure logs a warning but does NOT abort init (the
    settings/project are already written; the user can re-run / inspect).
    """
    if not state.storage or state.storage.db_backend != DbBackend.sqlite:
        return None
    db_path = state.storage.db_path
    if not db_path:
        return None
    db_path = str(Path(db_path).expanduser())

    resolved = Path(db_path)
    resolved.parent.mkdir(parents=True, exist_ok=True)

    # Throwaway seed password — random, never surfaced. OSS single-user never
    # authenticates with it (no login surface); it only unblocks migration 016.
    import secrets

    seed_password = secrets.token_urlsafe(32)

    # Heavy imports kept local so `marvis --help` / non-init paths stay fast and
    # never touch the DB layer. We set settings.db_path BEFORE importing
    # run_migrations so it migrates the user's configured file, not a default.
    prev_pwd = os.environ.get("PIR_PASSWORD")
    try:
        from core.api.config import settings

        settings.db_path = db_path
        os.environ["PIR_PASSWORD"] = seed_password
        from core.api.db import run_migrations

        run_migrations()
        return db_path
    except Exception as exc:  # noqa: BLE001 — never abort init on a migration hiccup
        err_console.print(
            f"[yellow]Schema bootstrap skipped ({exc}). "
            "Run will retry on first use; check the DB path is writable.[/yellow]"
        )
        return None
    finally:
        # Do not leak the throwaway password into the wider process environment.
        if prev_pwd is None:
            os.environ.pop("PIR_PASSWORD", None)
        else:
            os.environ["PIR_PASSWORD"] = prev_pwd


def _store_byok(
    state: WizardState, vault_dir: Path, *, allow_prompt: bool = False
) -> Path | None:
    if (
        not state.llm_provider
        or not state.llm_provider.provider
        or not state.llm_provider.api_key
    ):
        return None
    # When a passphrase source is available (env / keyring / TTY prompt), the
    # master key is written encrypted from the start; otherwise cleartext + a
    # one-time warning. Init never blocks on this.
    ensure_master_key(vault_dir, create=True, allow_prompt=allow_prompt)
    store_provider_key(
        state.llm_provider.provider.value,
        state.llm_provider.api_key,
        base_url=state.llm_provider.base_url,
        vault_dir=vault_dir,
    )
    return vault_dir / VAULT_FILENAME


# ---------------------------------------------------------------------------
# SP2 U4 — "make alive" consent asks + best-effort side effects
# ---------------------------------------------------------------------------


def _prompt_brain(
    *, interactive: bool, preset: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Collect the 3 onboarding consent choices that make a fresh install alive.

    Returns ``{opportunistic, reflection_mode, timer, hooks}``. A non-interactive
    run takes the preset (automation) or conservative all-off defaults — nothing
    that costs money, changes the environment, or starts background work happens
    without explicit consent (the brainstorm cardinal principle).
    """
    preset = preset or {}
    if not interactive:
        mode = preset.get("reflection_mode")
        return {
            "opportunistic": bool(preset.get("opportunistic", False)),
            "reflection_mode": mode if mode in ("free", "full") else "free",
            "timer": bool(preset.get("timer", False)),
            "hooks": bool(preset.get("hooks", False)),
        }

    console.print()
    console.rule("[bold]Make it alive — Company Brain")
    console.print(
        "Marvis can reflect in the background: digest what changed, surface drift, "
        "keep search fresh. Pick how:"
    )
    console.print("  [1] free  — local upkeep only, never uses your LLM key (default)")
    console.print("  [2] full  — adds LLM journal polish + citations (uses your key)")
    console.print("  [3] off   — no background reflection")
    choice = typer.prompt("Reflection", default="1", type=click.Choice(["1", "2", "3"]))
    if choice == "2":
        reflection_mode, opportunistic = "full", True
    elif choice == "3":
        reflection_mode, opportunistic = "free", False
    else:
        reflection_mode, opportunistic = "free", True

    timer = False
    if opportunistic:
        timer = typer.confirm(
            "Also reflect on a daily schedule even when you're not using marvis? "
            "(installs an OS timer)",
            default=False,
        )

    hooks = typer.confirm(
        "Install governance hooks into your Claude Code in this folder? "
        "(merges .claude/settings.json, reversible)",
        default=True,
    )
    return {
        "opportunistic": opportunistic,
        "reflection_mode": reflection_mode,
        "timer": timer,
        "hooks": hooks,
    }


def _apply_make_alive(choice: dict[str, Any]) -> None:
    """Best-effort side effects of the U4 asks. A failure logs, never aborts init."""
    if choice.get("timer"):
        try:
            from core.cli import _brain_schedule

            result = _brain_schedule.enable()
            if result.get("ok"):
                console.print(f"  brain timer → enabled ({result.get('backend')})")
                if result.get("linger_warning"):
                    err_console.print(f"    [yellow]{result['linger_warning']}[/yellow]")
            else:
                err_console.print(
                    f"  [yellow]brain timer not enabled: {result.get('error')}[/yellow]"
                )
        except Exception as exc:  # noqa: BLE001 — never abort init
            err_console.print(f"  [yellow]brain timer skipped ({exc}).[/yellow]")

    if choice.get("hooks"):
        try:
            _install_hooks_best_effort()
        except Exception as exc:  # noqa: BLE001 — never abort init
            err_console.print(f"  [yellow]hooks install skipped ({exc}).[/yellow]")


def _install_hooks_best_effort() -> None:
    """Invoke ``marvis hooks install`` (non-interactive, idempotent) as a subprocess."""
    import shutil

    exe = shutil.which("marvis")
    if not exe:
        err_console.print(
            "  [yellow]hooks install skipped: 'marvis' not on PATH. "
            "Run `marvis hooks install` later.[/yellow]"
        )
        return
    import subprocess

    proc = subprocess.run(  # noqa: S603 — fixed argv, no shell
        [exe, "hooks", "install"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if proc.returncode == 0:
        console.print("  governance hooks → installed (.claude/settings.json)")
    else:
        err_console.print(
            f"  [yellow]hooks install returned {proc.returncode}; "
            "run `marvis hooks install` manually.[/yellow]"
        )


@app.callback(invoke_without_command=True)
def _root(
    ctx: typer.Context,
    version: bool = typer.Option(
        False,
        "--version",
        help="Show the CLI version and exit.",
        is_eager=True,
    ),
) -> None:
    """Default to `init` when no subcommand provided.

    This is also the centralized telemetry chokepoint: every ``marvis`` invocation
    flows through here, so we emit a single anonymous ``cli_command`` event (the
    subcommand NAME only — a low-cardinality token, never args/paths) and show the
    one-time opt-out notice. The opt-out gate lives inside ``emit()`` /
    ``maybe_first_run_notice()`` (the single enforcement point); both are
    fail-silent, so telemetry can never block, slow, or error the command.
    """
    if version:
        typer.echo(CLI_VERSION)
        raise typer.Exit(code=0)

    _telemetry_root_hook(ctx.invoked_subcommand)
    _opportunistic_brain_hook(ctx.invoked_subcommand)
    _update_check_hook()
    if ctx.invoked_subcommand is None:
        ctx.invoke(init)


def _update_check_hook() -> None:
    """Print a cached "update available" notice (≤1×/day refresh), notify-only.

    Fail-silent: a missing/down PyPI or any error must never affect the command.
    """
    try:
        from core.cli import _update_check

        _update_check.maybe_notify_update()
    except Exception:  # noqa: BLE001 — update notice must never affect the command
        pass


def _telemetry_root_hook(invoked_subcommand: str | None) -> None:
    """Emit the per-invocation ``cli_command`` event + the first-run notice.

    Wrapped in a broad guard on top of ``emit()``'s own fail-silent contract: the
    telemetry import or call must NEVER break ``marvis``. ``MARVIS_TELEMETRY=log``
    routes the event to stderr (show-don't-send) inside ``emit()``.
    """
    try:
        from core.telemetry import client as _telemetry

        _telemetry.maybe_first_run_notice()
        _telemetry.emit("cli_command", {"command": invoked_subcommand or "init"})

        # Opportunistic daily aggregate rollup (provision + send), throttled 24h,
        # detached + fail-silent — same opt-out gate as emit(). Never blocks the command.
        from core.telemetry import sender as _sender

        _sender.maybe_send_rollup()
    except Exception:  # noqa: BLE001 — telemetry must never affect the command
        pass


def _opportunistic_brain_hook(invoked_subcommand: str | None) -> None:
    """Fire the ≤1×/day opportunistic Company-Brain upkeep (SP2 U2), detached.

    Skips when the invoked command IS ``brain`` (the spawned ``marvis brain run``
    itself → no self-recursion). Everything else — the enable gate, the 24h
    throttle, the detached spawn — lives in ``_brain_opportunistic`` and is
    fail-silent, so this can never block, slow, or error the command.
    """
    if invoked_subcommand == "brain":
        return
    try:
        from core.cli import _brain_opportunistic

        _brain_opportunistic.maybe_run_opportunistic_brain()
    except Exception:  # noqa: BLE001 — opportunistic upkeep must never affect the command
        pass


@app.command("init")
def init(
    projects_root: str | None = typer.Option(
        None, "--projects-root", help="Projects folder (absolute path)."
    ),
    db_backend: str | None = typer.Option(
        None,
        "--db-backend",
        help="Database backend: sqlite or postgres.",
    ),
    db_path: str | None = typer.Option(
        None, "--db-path", help="SQLite file path (when db_backend=sqlite)."
    ),
    postgres_dsn: str | None = typer.Option(
        None,
        "--postgres-dsn",
        help="Postgres DSN (when db_backend=postgres).",
    ),
    llm_provider: str | None = typer.Option(
        None,
        "--llm-provider",
        help="anthropic | openai | mac_gateway | bedrock | skip.",
    ),
    llm_api_key: str | None = typer.Option(
        None, "--llm-api-key", help="API key for the chosen provider."
    ),
    llm_base_url: str | None = typer.Option(
        None,
        "--llm-base-url",
        help="Base URL (required only for mac_gateway).",
    ),
    project_name: str | None = typer.Option(
        None, "--project-name", help="Name of the first project."
    ),
    project_slug: str | None = typer.Option(
        None,
        "--project-slug",
        help="Slug for the first project (kebab-case).",
    ),
    project_type: str | None = typer.Option(
        None, "--project-type", help="code | work | system."
    ),
    accept_bsl: bool = typer.Option(
        False,
        "--accept-bsl",
        help="Skip the BSL prompt by accepting the license explicitly.",
    ),
    no_interactive: bool = typer.Option(
        False,
        "--no-interactive",
        help="Disable prompts: all values must come from flags or --config.",
    ),
    config: Path | None = typer.Option(
        None,
        "--config",
        help="YAML preset with all answers (for CI / non-interactive).",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Show the plan without writing files or the vault.",
    ),
    vault_dir: Path | None = typer.Option(
        None,
        "--vault-dir",
        help="BYOK vault + settings.yaml folder (default $MARVIS_VAULT_DIR or ~/.marvis).",
    ),
    settings_path: Path | None = typer.Option(
        None,
        "--settings-path",
        help="Output path for settings.yaml (default <vault-dir>/settings.yaml).",
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Skip the final confirmation after the recap.",
    ),
) -> None:
    """5-step interactive bootstrap built on the shared setup library."""
    # When --vault-dir is omitted, honor $MARVIS_VAULT_DIR (else ~/.marvis) so
    # init never silently writes to the real home vault under isolation.
    if vault_dir is None:
        vault_dir = _default_vault_dir()
    interactive = _is_interactive() and not no_interactive
    brain_cfg: dict[str, Any] = {}

    if config is not None:
        preset = _load_preset(config)
        welcome_cfg = preset.get("welcome", {}) or {}
        storage_cfg = preset.get("storage", {}) or {}
        llm_cfg = preset.get("llm_provider", {}) or {}
        project_cfg = preset.get("first_project", {}) or {}
        accept_bsl = accept_bsl or bool(welcome_cfg.get("bsl_accepted"))
        projects_root = projects_root or storage_cfg.get("projects_root")
        db_backend = db_backend or storage_cfg.get("db_backend")
        db_path = db_path or storage_cfg.get("db_path")
        postgres_dsn = postgres_dsn or storage_cfg.get("postgres_dsn")
        llm_provider = llm_provider or llm_cfg.get("provider")
        llm_api_key = llm_api_key or llm_cfg.get("api_key")
        llm_base_url = llm_base_url or llm_cfg.get("base_url")
        project_name = project_name or project_cfg.get("name")
        project_slug = project_slug or project_cfg.get("slug")
        project_type = project_type or project_cfg.get("type")
        brain_cfg = preset.get("brain", {}) or {}

    state = WizardState()

    welcome = _prompt_welcome(accept_bsl=accept_bsl, interactive=interactive)
    state.welcome = welcome
    _validate_step(welcome)
    advance(state)

    storage = _prompt_storage(
        projects_root=projects_root,
        db_backend=db_backend,
        db_path=db_path,
        postgres_dsn=postgres_dsn,
        interactive=interactive,
    )
    state.storage = storage
    _validate_step(storage)
    advance(state)

    llm = _prompt_llm(
        provider=llm_provider,
        api_key=llm_api_key,
        base_url=llm_base_url,
        interactive=interactive,
    )
    state.llm_provider = llm
    _validate_step(llm)
    advance(state)

    first = _prompt_first_project(
        name=project_name,
        slug=project_slug,
        project_type=project_type,
        interactive=interactive,
    )
    state.first_project = first
    _validate_step(first)
    advance(state)

    # SP2 U4 — "make alive" consent asks (reflection cost-mode, autonomy timer, hooks).
    brain_choice = _prompt_brain(interactive=interactive, preset=brain_cfg)

    _print_recap(state)

    if interactive and not yes:
        confirmed = typer.confirm("Write these files now?", default=True)
        if not confirmed:
            console.print("[yellow]Aborted by user — no files written.[/yellow]")
            raise typer.Exit(code=2)

    finalize(state)
    settings_payload = _build_settings_dict(state)
    # SP2 U4 — persist the brain consent into settings.yaml; brain.opportunistic
    # is what arms the U2 on-invocation upkeep, brain.reflection_mode its mode.
    settings_payload["brain"] = {
        "opportunistic": brain_choice["opportunistic"],
        "reflection_mode": brain_choice["reflection_mode"],
    }
    settings_destination = settings_path or (vault_dir / DEFAULT_SETTINGS_FILENAME)

    if dry_run:
        console.print()
        console.rule("[bold]Dry-run plan")
        console.print(f"settings.yaml → {settings_destination}")
        console.print(f"byok.vault dir → {vault_dir}")
        if state.first_project and state.storage:
            project_yaml = (
                Path(state.storage.projects_root).expanduser()
                / state.first_project.slug
                / "project.yaml"
            )
            console.print(f"project.yaml → {project_yaml}")
        console.print()
        console.print("[cyan]settings.yaml contents:[/cyan]")
        console.print(
            yaml.safe_dump(settings_payload, sort_keys=False, allow_unicode=True)
        )
        raise typer.Exit(code=0)

    _write_settings(settings_destination, settings_payload)
    vault_file = _store_byok(state, vault_dir, allow_prompt=interactive)
    project_yaml = _seed_project(state)
    db_file = _bootstrap_schema(state)
    # SP2 U4 — best-effort make-alive side effects (timer + hooks), never aborts.
    _apply_make_alive(brain_choice)

    console.print()
    console.print("[green]Setup wizard complete.[/green]")
    console.print(f"  settings → {settings_destination}")
    if vault_file:
        console.print(f"  vault    → {vault_file}")
    if project_yaml:
        console.print(f"  project  → {project_yaml}")
    if db_file:
        console.print(f"  database → {db_file} (schema ready)")
    console.print()
    console.print("Next steps:")
    console.print("  - Check everything is healthy: marvis doctor")
    console.print(
        "  - Run the full Console + API stack (optional): "
        "https://github.com/emiliomartucci/marvisx-oss/tree/main/deploy/_template"
    )
    console.print("  - Docs: https://justaskmarvis.com")
    console.print()
    console.print("Bring an existing project into Marvis:")
    console.print("  - Read the conventions: marvis guide")
    console.print(
        '  - Then ask your agent: "Read `marvis guide`, look at this folder, and '
        "propose how to organize it into Marvis projects. Map and propose first — "
        "don't move anything without my OK — then create context.md and a handoff "
        'from the templates."'
    )
    console.print(
        "  - Register vs index: importing a project records metadata; to put its "
        "code in the knowledge graph, scaffold it and run "
        "`marvis project index <slug>`."
    )


@app.command("version")
def version_cmd() -> None:
    """Print the CLI version."""
    typer.echo(CLI_VERSION)


# Runtime subcommands (status/brief/project/triage/approve/audit) live in
# marvis_runtime and register onto THIS app, so `marvis` stays a single
# entrypoint. Heavy imports inside marvis_runtime are lazy (per-command), so
# importing the registration module here does not slow `marvis --help`.
from core.cli.marvis_runtime import register as _register_runtime  # noqa: E402

_register_runtime(app)

# `marvis hooks install/uninstall/status` — ship the de-hardcoded governance hooks.
from core.cli.marvis_hooks import register as _register_hooks  # noqa: E402

_register_hooks(app)

# `marvis governance lite/strict/status` — switch installed hook enforcement profile.
from core.cli.marvis_governance import register as _register_governance  # noqa: E402

_register_governance(app)

# `marvis mcp register/status` — wire the Marvis MCP server into the user's .mcp.json.
from core.cli.marvis_mcp import register as _register_mcp  # noqa: E402

_register_mcp(app)

# `marvis telemetry on/off/status/log` — the anonymous opt-out telemetry control.
from core.cli.marvis_telemetry import register as _register_telemetry  # noqa: E402

_register_telemetry(app)

# `marvis account status/link` — opt-in identity: link this install to a web account.
from core.cli.marvis_account import register as _register_account  # noqa: E402

_register_account(app)

# `marvis doctor` — install-health self-diagnostic with actionable remediation.
from core.cli.marvis_doctor import register as _register_doctor  # noqa: E402

_register_doctor(app)

# `marvis feedback` — open a GitHub issue on the public OSS repo (gh → URL).
from core.cli.marvis_feedback import register as _register_feedback  # noqa: E402

_register_feedback(app)

# `marvis brain run` — run one Company-Brain reflection cycle on demand
# (free upkeep floor / full LLM polish). The U2 opportunistic + U3 timer entry.
from core.cli.marvis_brain import register as _register_brain  # noqa: E402

_register_brain(app)

# `marvis guide` — the single callable "Marvis way" reference guide. Same content
# published on the web, so an agent can read it before installing anything.
from core.cli.marvis_guide import register as _register_guide  # noqa: E402

_register_guide(app)

# `marvis task list/show/approve/reject` — the human task surface from the terminal
# (thin adapter over the task use_cases; local single-user can approve directly).
from core.cli.marvis_task import register as _register_task  # noqa: E402

_register_task(app)


@app.command("show-state")
def show_state_cmd(
    config: Path = typer.Argument(..., help="Path to a YAML preset."),
) -> None:
    """Load a preset and print the wizard state as JSON (debug)."""
    preset = _load_preset(config)
    typer.echo(json.dumps(preset, indent=2, default=str))


def main() -> None:
    """Console-script entry point. Makes CLI output Windows-safe (UTF-8 stdout/
    stderr) before dispatching, so non-ASCII output never raises
    UnicodeEncodeError on a legacy cp1252 console."""
    from core.platform import ensure_utf8_io

    ensure_utf8_io()
    app()


if __name__ == "__main__":  # pragma: no cover
    main()
