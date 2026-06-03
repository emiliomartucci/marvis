# v1.0.0 - 2026-06-03 - SP2 U1: `marvis brain run` — one-shot reflection cycle
"""``marvis brain`` — run the Company-Brain reflection cycle on demand.

``marvis brain run`` executes ONE reflection cycle in-process, then exits. It is
the CLI entry the U3 timer and the U2 opportunistic trigger invoke; it shares
the SAME cycle code path as the server scheduler (``brain/jobs.py``) and never
reimplements the cycle.

Modes (reflection cost-mode, SP2 decision #3):
- ``--mode free`` (default): the no-LLM upkeep floor — substrate digest,
  deterministic drift / memory-ops / findings, journals — WITHOUT the LLM
  journal-polish phase. Never touches a BYOK key.
- ``--mode full``: adds the LLM journal polish (+ F5 source citations) when a
  brain LLM gateway is configured; degrades to the deterministic narrative when
  it is not.

Registered onto the SAME Typer ``app`` as ``marvis init`` via ``register(app)``,
exactly like ``marvis_doctor`` / ``marvis_telemetry``. Heavy imports (the brain
cycle + DB) stay INSIDE the command body so ``marvis --help`` never pays for them.
"""
from __future__ import annotations

from typing import Any

import typer

from core.cli._runtime_ctx import emit, err_console, run_async

_PANEL_BRAIN = "Company Brain"

brain_app = typer.Typer(add_completion=False, no_args_is_help=True)


def register(app: typer.Typer) -> None:
    """Attach the ``brain`` command group onto an existing app."""
    app.add_typer(
        brain_app,
        name="brain",
        rich_help_panel=_PANEL_BRAIN,
        help="Run the Company-Brain reflection cycle (free upkeep / full LLM polish).",
    )


@brain_app.command("run")
def run_cmd(
    mode: str = typer.Option(
        "free",
        "--mode",
        "-m",
        help=(
            "free = no-LLM upkeep floor (default); "
            "full = adds LLM journal polish + citations."
        ),
    ),
    json_out: bool = typer.Option(
        False, "--json", help="Emit pure JSON to stdout."
    ),
) -> None:
    """Run one reflection cycle now, then exit."""
    if mode not in ("free", "full"):
        err_console.print(
            f"[red]invalid --mode {mode!r}: choose 'free' or 'full'.[/red]"
        )
        raise typer.Exit(2)

    # Point the runtime at the user's configured DB + projects_root BEFORE any
    # DB access, then lazily bring up the single-writer pool (the cycle writes).
    from core.cli._runtime_ctx import _apply_settings

    _apply_settings()

    async def _run() -> dict[str, Any]:
        from core.api import db as db_mod

        if db_mod._writer is None:  # lazy one-shot writer init for the CLI process
            await db_mod.init_pool()

        from core.api.services.brain.jobs import run_brain_cycle_once

        return await run_brain_cycle_once(reflection_mode=mode)

    payload = run_async(_run())
    emit(payload, json_out=json_out, render=_render)


def _render(payload: dict[str, Any]) -> None:
    """Compact human summary of a cycle run (stdout; warnings go to stderr)."""
    from core.cli._runtime_ctx import console

    status = payload.get("status", "unknown")
    reflection_mode = payload.get("reflection_mode", "?")

    if status == "disabled":
        console.print(
            "[yellow]Brain is disabled (brain_enabled=false). "
            "Enable it in settings to run a reflection cycle.[/yellow]"
        )
        return
    if status == "already_running":
        console.print(
            f"[yellow]A cycle is already running for "
            f"{payload.get('cycle_key', '?')} "
            f"(since {payload.get('lease_started_at', '?')}). Skipped.[/yellow]"
        )
        return

    style = {
        "ok": "green",
        "partial": "yellow",
        "failed": "red",
    }.get(status, "white")

    console.print(
        f"[{style}]brain cycle {status}[/{style}] "
        f"(mode={reflection_mode}, cycle={payload.get('cycle_key', '?')})"
    )
    console.print(
        f"  events={payload.get('event_count', 0)} "
        f"journals={payload.get('journal_count', 0)} "
        f"duration_ms={payload.get('duration_ms', 0)}"
    )

    failures = payload.get("partial_failures") or []
    if failures:
        console.print(f"  [yellow]partial failures ({len(failures)}):[/yellow]")
        for f in failures:
            console.print(
                f"    - {f.get('source_system', '?')}: {f.get('error', '?')}"
            )
    if payload.get("error"):
        console.print(f"  [red]error: {payload['error']}[/red]")


@brain_app.command("schedule")
def schedule_cmd(
    enable: bool = typer.Option(
        False, "--enable", help="Install the daily reflection timer (launchd/systemd/cron)."
    ),
    disable: bool = typer.Option(
        False, "--disable", help="Remove the reflection timer."
    ),
    show_status: bool = typer.Option(
        False, "--status", help="Report the real OS-level timer state (default)."
    ),
    json_out: bool = typer.Option(False, "--json", help="Emit pure JSON to stdout."),
) -> None:
    """Manage the OS-native daily timer that runs ``marvis brain run --mode full``.

    Cross-OS: macOS launchd LaunchAgent (runs on wake) / Linux systemd --user
    timer (Persistent + linger) / cron fallback. The agent INVOKES this — it
    never hand-writes the timer artifacts.
    """
    if sum(bool(x) for x in (enable, disable, show_status)) > 1:
        err_console.print(
            "[red]choose exactly one of --enable / --disable / --status.[/red]"
        )
        raise typer.Exit(2)

    from core.cli import _brain_schedule

    if enable:
        result = _brain_schedule.enable()
    elif disable:
        result = _brain_schedule.disable()
    else:  # default → status
        result = _brain_schedule.status()
    result["action"] = "enable" if enable else "disable" if disable else "status"

    emit(result, json_out=json_out, render=_render_schedule)


def _render_schedule(result: dict[str, Any]) -> None:
    from core.cli._runtime_ctx import console

    backend = result.get("backend", "?")
    action = result.get("action", "status")

    if backend == "unsupported":
        console.print(f"[yellow]{result.get('error', 'scheduling unsupported here')}[/yellow]")
        return

    if action == "status":
        en = result.get("enabled")
        style = "green" if en else "yellow"
        console.print(f"[{style}]brain schedule ({backend}): {'enabled' if en else 'not enabled'}[/{style}]")
        for k in ("active", "linger", "units_present", "plist_present"):
            if k in result:
                console.print(f"  {k}={result[k]}")
        return

    if result.get("ok"):
        console.print(f"[green]brain schedule {action}d ({backend}).[/green]")
        if result.get("linger_warning"):
            console.print(f"  [yellow]{result['linger_warning']}[/yellow]")
    else:
        console.print(
            f"[red]brain schedule {action} failed ({backend}): "
            f"{result.get('error', 'unknown')}[/red]"
        )
        raise typer.Exit(1)
