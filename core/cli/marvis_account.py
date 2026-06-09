# v1.0.0 - 2026-05-29 - open-core funnel: `marvis account status|link` (opt-in identity)
"""``marvis account status|link`` — the opt-in account / install-claim surface.

Registered onto the SAME Typer ``app`` as ``marvis init`` via ``register(app)``, like
``marvis_telemetry`` / ``marvis_hooks`` / ``marvis_mcp``.

- ``status`` is local-only (no network): shows whether an ``install_id`` and a cloud
  api key exist (presence, not values), the configured endpoints, and the effective
  telemetry state. Safe to paste into a bug report.
- ``link`` is the explicit opt-in: it provisions the install with the cloud backend
  (mints + stores an api key once) and prints the **claim code** + the dashboard URL.
  The user signs in at the dashboard and pastes the code to attach this install to
  their account (the web side calls ``/v1/installs/claim``). Telemetry being off does
  NOT block ``link`` — it is a deliberate user action, distinct from passive telemetry.

Heavy work is avoided: ``status`` only reads ``~/.marvis``; ``link`` does one short,
fail-silent provisioning POST (≤2s) reusing the sender's contract.
"""
from __future__ import annotations

from typing import Any

import typer

from core.cli._runtime_ctx import console, emit as _emit_result

ACCOUNT_URL = "https://justaskmarvis.com/account"
_PANEL_ACCOUNT = "Account"


def register(app: typer.Typer) -> None:
    """Attach the ``account`` command group onto an existing app."""
    app.add_typer(
        account_app,
        name="account",
        rich_help_panel=_PANEL_ACCOUNT,
        help="Link this install to a justaskmarvis.com account (opt-in) / inspect it.",
    )


account_app = typer.Typer(add_completion=False, no_args_is_help=True)


def _claim_code() -> str | None:
    from core.telemetry import client as _tc

    try:
        path = _tc._marvis_dir() / "telemetry_claim_code"
        if path.is_file():
            value = path.read_text(encoding="utf-8").strip()
            return value or None
    except Exception:  # noqa: BLE001
        return None
    return None


def _status_result() -> dict[str, Any]:
    """Local-only account snapshot (no network)."""
    from core.telemetry import client as _tc
    from core.telemetry import sender as _sender

    return {
        "telemetry_enabled": _tc._enabled(),
        "install_id_present": (_tc._marvis_dir() / "telemetry_id").is_file(),
        "account_provisioned": _sender._load_key() is not None,
        "claim_code_present": _claim_code() is not None,
        "ingest_endpoint": _sender._endpoint("ingest_endpoint", _sender.DEFAULT_INGEST_ENDPOINT),
        "provision_endpoint": _sender._endpoint("provision_endpoint", _sender.DEFAULT_PROVISION_ENDPOINT),
        "account_url": ACCOUNT_URL,
    }


def _link_result() -> dict[str, Any]:
    """Provision the install (mint+store a key once) and return the claim code. One short POST."""
    from core.telemetry import client as _tc
    from core.telemetry import sender as _sender

    key = _sender._ensure_key(_tc._install_id())
    return {
        "provisioned": key is not None,
        "claim_code": _claim_code() if key is not None else None,
        "account_url": ACCOUNT_URL,
        "provision_endpoint": _sender._endpoint("provision_endpoint", _sender.DEFAULT_PROVISION_ENDPOINT),
    }


@account_app.command("status")
def status_cmd(
    json_out: bool = typer.Option(False, "--json", help="Emit pure JSON to stdout."),
) -> None:
    """Show the local account state (presence only, no values; no network)."""
    result = _status_result()

    def _render(r: dict[str, Any]) -> None:
        from rich.table import Table

        t = Table(title="marvis account status", show_header=False)
        t.add_row("Telemetry", "[green]on[/]" if r["telemetry_enabled"] else "[yellow]off[/]")
        t.add_row("install_id", "[green]present[/]" if r["install_id_present"] else "[dim]not yet created[/]")
        t.add_row("Account", "[green]provisioned[/]" if r["account_provisioned"] else "[dim]not linked[/]")
        t.add_row("Claim code", "[green]present[/]" if r["claim_code_present"] else "[dim]none[/]")
        t.add_row("Ingest", r["ingest_endpoint"])
        console.print(t)
        if not r["account_provisioned"]:
            console.print(f"[dim]Run [bold]marvis account link[/] to link this install at {r['account_url']}.[/]")

    _emit_result(result, json_out=json_out, render=_render)


@account_app.command("link")
def link_cmd(
    json_out: bool = typer.Option(False, "--json", help="Emit pure JSON to stdout."),
) -> None:
    """Provision this install and print the claim code to link it to your account."""
    result = _link_result()

    def _render(r: dict[str, Any]) -> None:
        if not r["provisioned"]:
            console.print(
                f"[yellow]Could not reach {r['provision_endpoint']}.[/] "
                "Check your connection and retry — nothing was changed."
            )
            return
        code = r["claim_code"]
        console.print("[green]Install provisioned.[/] To link it to your account:")
        console.print(f"  1. Sign in at [bold]{r['account_url']}[/]")
        if code:
            console.print(f"  2. Paste this claim code: [bold]{code}[/]")
        else:
            console.print("  2. Use 'claim install' in the dashboard (claim code issued at first provision).")

    _emit_result(result, json_out=json_out, render=_render)
