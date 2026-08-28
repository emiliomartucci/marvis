"""Receipt-backed offline schema commands for the local single-user runtime."""
from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

import typer

from core.cli._runtime_ctx import console, err_console


schema_app = typer.Typer(add_completion=False, no_args_is_help=True)


def register(app: typer.Typer) -> None:
    app.add_typer(
        schema_app,
        name="schema",
        help="Upgrade or restore the local SQLite schema with a durable receipt.",
    )


def _default_release_id() -> str:
    try:
        return f"marvis-oss@{version('marvisx-cli')}"
    except PackageNotFoundError:
        return "marvis-oss@dev"


def _prepare() -> str:
    from core.api.runtime_settings import apply_marvis_settings
    from core.api.services import schema_upgrade

    apply_marvis_settings()
    return schema_upgrade.prove_local_writers_stopped()


def _emit(receipt: object, *, json_out: bool) -> None:
    from core.api.services import schema_upgrade

    if json_out:
        typer.echo(schema_upgrade.receipt_as_json(receipt))
        return
    console.print(
        f"[green]{receipt.status}[/]  schema "
        f"v{receipt.initial_version} → v{receipt.final_version}"
    )
    console.print(f"[dim]release: {receipt.release_id}[/]")
    if receipt.backup_path:
        console.print(f"[dim]backup: {receipt.backup_path}[/]")


@schema_app.command("upgrade")
def upgrade_cmd(
    release_id: str | None = typer.Option(
        None,
        "--release-id",
        help="Immutable package/release identifier; defaults to the installed version.",
    ),
    receipt: Path | None = typer.Option(
        None,
        "--receipt",
        help="Absolute receipt path; the default is beside the database backups.",
    ),
    json_out: bool = typer.Option(False, "--json", help="Emit pure JSON."),
) -> None:
    """Stop-on-contention offline upgrade with backup and immutable receipt."""

    from core.api.services import schema_upgrade

    try:
        proof = _prepare()
        result = schema_upgrade.run_controlled_upgrade(
            release_id or _default_release_id(),
            proof_kind=proof,
            receipt_path=receipt,
        )
    except Exception as exc:  # noqa: BLE001 - CLI boundary emits closed failure
        err_console.print(f"[red]Schema upgrade refused:[/] {exc}")
        raise typer.Exit(1) from exc
    _emit(result, json_out=json_out)


@schema_app.command("restore")
def restore_cmd(
    release_id: str | None = typer.Option(
        None,
        "--release-id",
        help="The exact release identifier stored by the upgrade receipt.",
    ),
    receipt: Path | None = typer.Option(
        None,
        "--receipt",
        help="Absolute receipt path; the default is beside the database backups.",
    ),
    yes: bool = typer.Option(False, "--yes", help="Confirm database replacement."),
    json_out: bool = typer.Option(False, "--json", help="Emit pure JSON."),
) -> None:
    """Restore the exact pre-upgrade backup; all local writers must be stopped."""

    from core.api.services import schema_upgrade

    if not yes:
        err_console.print("[red]Restore refused:[/] pass --yes after stopping Marvis.")
        raise typer.Exit(2)
    try:
        proof = _prepare()
        result = schema_upgrade.restore_controlled_upgrade(
            release_id or _default_release_id(),
            proof_kind=proof,
            receipt_path=receipt,
        )
    except Exception as exc:  # noqa: BLE001 - CLI boundary emits closed failure
        err_console.print(f"[red]Schema restore refused:[/] {exc}")
        raise typer.Exit(1) from exc
    _emit(result, json_out=json_out)
