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


# ---------------------------------------------------------------------------
# brain_enabled setter (issue #7) — a supported toggle instead of a direct
# console.db UPDATE (which the governance hooks block).
# ---------------------------------------------------------------------------


async def _read_brain_enabled() -> str:
    """Current brain_enabled mode; defaults to 'shadow' like the runtime loader."""
    from core.cli._runtime_ctx import with_db

    async with with_db() as db:
        cur = await db.execute(
            "SELECT value FROM app_settings WHERE key = 'brain_enabled'"
        )
        row = await cur.fetchone()
    return row[0] if row and row[0] else "shadow"


async def _write_brain_enabled(value: str) -> None:
    """Set brain_enabled through the single writer (the supported path) and close
    the pool we opened so the one-shot CLI exits immediately (no ~30s teardown)."""
    from datetime import datetime, timezone

    from core.cli._runtime_ctx import _apply_settings

    _apply_settings()
    from core.api import db as db_mod

    own_pool = db_mod._writer is None
    if own_pool:
        await db_mod.init_pool()
    try:
        async with db_mod.acquire_write_db(label="marvis-brain-set") as db:
            await db.execute(
                "INSERT INTO app_settings (key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
                "updated_at = excluded.updated_at",
                ("brain_enabled", value, datetime.now(timezone.utc).isoformat()),
            )
            await db.commit()
    finally:
        if own_pool:
            await db_mod.close_pool()


def _print_brain_mode(mode: str) -> None:
    from core.cli._runtime_ctx import console

    style = {"true": "green", "shadow": "cyan", "false": "yellow"}.get(mode, "white")
    explain = {
        "true": "on — reflection runs and writes",
        "shadow": "shadow — reflection runs, proposals only (default)",
        "false": "off — reflection disabled",
    }.get(mode, mode)
    console.print(f"[{style}]brain_enabled = {mode}[/{style}]  ({explain})")


@brain_app.command("enable")
def enable_cmd(
    shadow: bool = typer.Option(
        False, "--shadow", help="Enable in shadow mode (proposals only) instead of full."
    ),
    json_out: bool = typer.Option(False, "--json", help="Emit pure JSON to stdout."),
) -> None:
    """Turn the Company Brain on — a supported setter, no direct DB write."""
    mode = "shadow" if shadow else "true"
    run_async(_write_brain_enabled(mode))
    emit(
        {"brain_enabled": mode},
        json_out=json_out,
        render=lambda r: _print_brain_mode(r["brain_enabled"]),
    )


@brain_app.command("disable")
def disable_cmd(
    json_out: bool = typer.Option(False, "--json", help="Emit pure JSON to stdout."),
) -> None:
    """Turn the Company Brain off."""
    run_async(_write_brain_enabled("false"))
    emit(
        {"brain_enabled": "false"},
        json_out=json_out,
        render=lambda r: _print_brain_mode(r["brain_enabled"]),
    )


@brain_app.command("status")
def brain_status_cmd(
    json_out: bool = typer.Option(False, "--json", help="Emit pure JSON to stdout."),
) -> None:
    """Show whether the Company Brain is on, off, or in shadow mode."""
    mode = run_async(_read_brain_enabled())
    emit(
        {"brain_enabled": mode},
        json_out=json_out,
        render=lambda r: _print_brain_mode(r["brain_enabled"]),
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
    if_due: bool = typer.Option(
        False,
        "--if-due",
        help=(
            "Scheduled-entry semantics (timers/cron): skip as 'idle' when "
            "today's cycle is already published or before the cutoff, instead "
            "of force-running (and superseding) it. Ignores --mode: the "
            "scheduler path always runs the server default. A plain `run` "
            "always runs the current cycle."
        ),
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

        # We own the pool only if we open it here. A server-hosted process that
        # already initialized the pool keeps owning it — we must not tear it down.
        own_pool = db_mod._writer is None
        if own_pool:  # lazy one-shot writer init for the CLI process
            await db_mod.init_pool()

        # --if-due routes to the idempotent scheduler entry (herd rule, task
        # 64b1eee3): a timer tick after the in-process API scheduler already
        # published the cycle must be a no-op, not a superseding re-run.
        if if_due:
            from core.api.services.brain.jobs import run_brain_jobs_if_due

            try:
                return await run_brain_jobs_if_due()
            finally:
                if own_pool:
                    await db_mod.close_pool()

        from core.api.services.brain.jobs import run_brain_cycle_once

        try:
            return await run_brain_cycle_once(reflection_mode=mode)
        finally:
            # Close the connections we opened. Each aiosqlite connection runs on a
            # NON-daemon thread that the interpreter joins at exit, so leaving the
            # writer open hangs the one-shot CLI ~30s after run_async returns —
            # the flagship-command "is it broken?" bug (issue #1).
            if own_pool:
                await db_mod.close_pool()

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
