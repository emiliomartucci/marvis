# v1.0.0 - 2026-05-27 - S2 F5: `marvis telemetry` on/off/status/log
"""``marvis telemetry on|off|status|log`` — the opt-in control surface.

Registered onto the SAME Typer ``app`` as ``marvis init`` via ``register(app)``,
exactly like ``marvis_hooks`` / ``marvis_mcp`` / ``marvis_runtime``.

- ``on`` / ``off`` write ``telemetry: true|false`` into ``~/.marvis/settings.yaml``
  (preserving every other key). This is the persistent layer — the env vars
  (``DO_NOT_TRACK`` / ``MARVIS_TELEMETRY``) still override it at runtime.
- ``status`` shows the EFFECTIVE state + which precedence layer decided it +
  whether an ``install_id`` exists (presence only — never the value, to avoid
  pasting a stable id into logs/screenshots needlessly).
- ``log`` is a hint/alias: it explains the env-driven show-don't-send mode
  (``MARVIS_TELEMETRY=log``) which prints events to stderr without sending. We do
  NOT mutate the environment from a command (it would not outlive the process);
  the command tells the user exactly how to enable it for a run.

Heavy work is avoided: this only reads/writes ``settings.yaml`` (YAML) and reads
the env. No DB, no network, no model load.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import typer
import yaml

from core.cli._runtime_ctx import console, emit as _emit_result
from core.platform import secure_path

_PANEL_TELEMETRY = "Telemetry"


def register(app: typer.Typer) -> None:
    """Attach the ``telemetry`` command group onto an existing app."""
    app.add_typer(
        telemetry_app,
        name="telemetry",
        rich_help_panel=_PANEL_TELEMETRY,
        help="Turn anonymous telemetry on / off / inspect (opt-in, no PII).",
    )


telemetry_app = typer.Typer(add_completion=False, no_args_is_help=True)


# ---------------------------------------------------------------------------
# settings.yaml helpers (mirror the path resolution used across the CLI)
# ---------------------------------------------------------------------------


def _settings_path() -> Path:
    settings_path = os.environ.get("MARVIS_SETTINGS_PATH")
    if settings_path:
        return Path(settings_path).expanduser()
    vault_dir = os.environ.get("MARVIS_VAULT_DIR")
    base = Path(vault_dir).expanduser() if vault_dir else Path.home() / ".marvis"
    return base / "settings.yaml"


def _read_settings(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001
        return {}
    return data if isinstance(data, dict) else {}


def _write_telemetry_flag(enabled: bool) -> Path:
    """Persist ``telemetry: <bool>`` in settings.yaml, preserving every other key."""
    path = _settings_path()
    data = _read_settings(path)
    # Collapse any nested {enabled: ...} shape into the bare bool we standardize on.
    data["telemetry"] = enabled
    path.parent.mkdir(parents=True, exist_ok=True)
    secure_path(path.parent, mode=0o700)
    path.write_text(
        yaml.safe_dump(data, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    secure_path(path, mode=0o600)
    return path


# ---------------------------------------------------------------------------
# marvis telemetry on / off
# ---------------------------------------------------------------------------


@telemetry_app.command("on")
def on_cmd(
    json_out: bool = typer.Option(False, "--json", help="Emit pure JSON to stdout."),
) -> None:
    """Enable anonymous telemetry (writes ``telemetry: true`` to settings.yaml)."""
    path = _write_telemetry_flag(True)
    result = {"telemetry": True, "settings": str(path)}

    def _render(r: dict[str, Any]) -> None:
        console.print(f"[green]telemetry on[/] → {r['settings']}")
        console.print(
            "  [dim]env DO_NOT_TRACK / MARVIS_TELEMETRY still override this at runtime[/]"
        )

    _emit_result(result, json_out=json_out, render=_render)


@telemetry_app.command("off")
def off_cmd(
    json_out: bool = typer.Option(False, "--json", help="Emit pure JSON to stdout."),
) -> None:
    """Disable anonymous telemetry (writes ``telemetry: false`` to settings.yaml)."""
    path = _write_telemetry_flag(False)
    result = {"telemetry": False, "settings": str(path)}

    def _render(r: dict[str, Any]) -> None:
        console.print(f"[yellow]telemetry off[/] → {r['settings']}")

    _emit_result(result, json_out=json_out, render=_render)


# ---------------------------------------------------------------------------
# marvis telemetry status
# ---------------------------------------------------------------------------


@telemetry_app.command("status")
def status_cmd(
    json_out: bool = typer.Option(False, "--json", help="Emit pure JSON to stdout."),
) -> None:
    """Show the effective telemetry state + which precedence layer decided it."""
    from core.telemetry import client as _tc

    # Determine the deciding layer WITHOUT duplicating the gate logic.
    do_not_track = bool(os.environ.get("DO_NOT_TRACK"))
    env_val = os.environ.get("MARVIS_TELEMETRY", "").lower()

    if do_not_track:
        deciding = "env:DO_NOT_TRACK"
        enabled = False
    elif env_val in ("0", "off", "false"):
        deciding = "env:MARVIS_TELEMETRY"
        enabled = False
    elif env_val == "log":
        deciding = "env:MARVIS_TELEMETRY=log"
        enabled = True
    else:
        deciding = "settings.yaml"
        enabled = _tc._settings_telemetry_on()

    settings_path = _settings_path()
    install_id_present = (_tc._marvis_dir() / "telemetry_id").is_file()

    result = {
        "enabled": enabled,
        "log_mode": env_val == "log",
        "deciding_layer": deciding,
        "precedence": [
            "DO_NOT_TRACK (set → off)",
            "MARVIS_TELEMETRY in {0,off,false} → off | log → show-don't-send | else fall through",
            "settings.yaml telemetry: false → off",
            "default → off",
        ],
        "settings": str(settings_path),
        "endpoint": _tc._endpoint(),
        "install_id_present": install_id_present,
    }

    def _render(r: dict[str, Any]) -> None:
        from rich.table import Table

        t = Table(title="marvis telemetry status", show_header=False)
        state = "[green]on[/]" if r["enabled"] else "[yellow]off[/]"
        if r["log_mode"]:
            state = "[cyan]log (show, don't send)[/]"
        t.add_row("State", state)
        t.add_row("Decided by", r["deciding_layer"])
        t.add_row("Settings", r["settings"])
        t.add_row("Endpoint", r["endpoint"])
        t.add_row(
            "install_id",
            "[green]present[/]" if r["install_id_present"] else "[dim]not yet created[/]",
        )
        console.print(t)
        console.print("[dim]Precedence (any opt-out wins):[/]")
        for i, layer in enumerate(r["precedence"], 1):
            console.print(f"  {i}. {layer}")

    _emit_result(result, json_out=json_out, render=_render)


# ---------------------------------------------------------------------------
# marvis telemetry log
# ---------------------------------------------------------------------------


@telemetry_app.command("log")
def log_cmd(
    json_out: bool = typer.Option(False, "--json", help="Emit pure JSON to stdout."),
) -> None:
    """Explain show-don't-send mode (``MARVIS_TELEMETRY=log``): print, never send.

    This does not change persistent state — env mode lasts only for the process
    that sets it. It is the strongest trust signal: you *see* the exact JSON we
    would transmit, with zero network calls, and can verify there is no PII.
    """
    result = {
        "mode": "log",
        "env": "MARVIS_TELEMETRY=log",
        "behavior": "prints each event JSON to stderr, sends nothing",
    }

    def _render(r: dict[str, Any]) -> None:
        console.print("[cyan]show-don't-send mode[/] — see events without sending any:")
        console.print("  [bold]MARVIS_TELEMETRY=log marvis status[/]")
        console.print(
            "  [dim]Each event is printed to stderr as JSON; no network call is made.[/]"
        )

    _emit_result(result, json_out=json_out, render=_render)
