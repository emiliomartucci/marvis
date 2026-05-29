# v1.0.0 - 2026-05-27 - S2 F4: `marvis mcp` — register / status for the PiR MCP server
"""``marvis mcp register`` / ``marvis mcp status`` — wire the Python MCP server into
the user's ``.mcp.json`` (merge) and verify it actually responds.

Registered onto the SAME Typer ``app`` as ``marvis init`` via ``register(app)``,
exactly like ``marvis_hooks`` / ``marvis_runtime``.

What ``register`` does (plan S2 F4 §"`.mcp.json` corretto"):

1. Resolve target ``.mcp.json`` (default ``<cwd>/.mcp.json``).
2. **Merge ONLY the ``pir`` key** into ``mcpServers``: absent → add; present-same
   → no-op; present-different → replace ONLY ``pir``. Every other server the user
   has is preserved untouched.
3. **Backup** the target → ``.mcp.<timestamp>.bak`` before any write.
4. Atomic write: tmp + ``os.replace`` with a ``json.loads`` validation guard.
5. ``--dry-run`` prints the entry + diff without writing.

The entry shape is correct by construction (these are documented recurring bugs):

  * ``"command"`` = the **resolved interpreter** ``sys.executable`` — NEVER a bare
    ``"python"``. Many systems only have ``python3`` / a venv; a bare ``python``
    registers a server that is dead on spawn (ENOENT). Using ``sys.executable``
    means the server runs under the SAME interpreter where ``marvisx-cli`` is
    installed, so "the CLI works but the MCP doesn't" can't happen.
  * ``"args"`` = the ARRAY ``["-m", "core.api.mcp.server"]`` — NEVER a single
    string ``"python -m core.api.mcp.server"`` as ``command`` (Claude Code would
    ``spawn`` a literal binary by that name → ENOENT, issue #590).
  * config (db path / OSS flag) flows via ``args`` flags, NOT via ``env``: the
    ``env`` of ``.mcp.json`` is an unreliable path to a stdio child (issue #38381).
    The entry ships ``"env": {}`` and the server reads ``~/.marvis/settings.yaml``
    on its own (the ``_apply_settings`` path), so no env coupling is needed.

``marvis mcp status`` does a REAL ``tools/list`` round-trip: it spawns the
configured server (the exact ``command`` + ``args`` from ``.mcp.json``) as a stdio
subprocess and runs the MCP ``initialize`` → ``tools/list`` handshake. Reading the
file only tells you it is *registered*; the round-trip tells you it *responds* —
catching the "registered but dead interpreter" class the plan calls out.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import typer

from core.cli._runtime_ctx import console, emit, err_console

# ---------------------------------------------------------------------------
# The single server we own in the user's .mcp.json. `pir` is the merge key:
# we touch ONLY this entry, never any other server the user has registered.
# ---------------------------------------------------------------------------

_SERVER_KEY = "pir"
_SERVER_MODULE = "core.api.mcp.server"
_PANEL_MCP = "MCP"


def register(app: typer.Typer) -> None:
    """Attach the ``mcp`` command group onto an existing app."""
    app.add_typer(
        mcp_app,
        name="mcp",
        rich_help_panel=_PANEL_MCP,
        help="Register / inspect the PiR MCP server in your .mcp.json.",
    )


mcp_app = typer.Typer(add_completion=False, no_args_is_help=True)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_target(config: str | None) -> Path:
    """Default = ``<cwd>/.mcp.json``; ``--config`` overrides."""
    if config:
        return Path(config).expanduser()
    return Path.cwd() / ".mcp.json"


def _pir_entry() -> dict[str, Any]:
    """The canonical ``pir`` server entry.

    ``command`` is the resolved interpreter (``sys.executable``) and ``args`` is an
    ARRAY — the two correctness invariants that keep the server from being
    "registered but dead". ``env`` stays empty on purpose (config is read from
    ``~/.marvis/settings.yaml`` by the server, not injected via the unreliable
    stdio ``env`` path).
    """
    return {
        "type": "stdio",
        "command": sys.executable,
        "args": ["-m", _SERVER_MODULE],
        "env": {},
    }


def _load_config(target: Path) -> dict[str, Any]:
    if not target.is_file():
        return {}
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _merge_pir(config: dict[str, Any]) -> tuple[dict[str, Any], str, dict[str, Any]]:
    """Merge ONLY the ``pir`` key into ``mcpServers``. Returns (new, action, entry).

    ``action`` ∈ {``add``, ``replace``, ``skip-present``}. Never rebuilds
    ``mcpServers`` wholesale — every non-``pir`` server is preserved verbatim.
    """
    # Deep copy so we never mutate the caller's dict.
    new = json.loads(json.dumps(config))
    servers = new.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        servers = {}
        new["mcpServers"] = servers

    desired = _pir_entry()
    existing = servers.get(_SERVER_KEY)
    if existing is None:
        servers[_SERVER_KEY] = desired
        action = "add"
    elif existing == desired:
        action = "skip-present"
    else:
        servers[_SERVER_KEY] = desired
        action = "replace"
    return new, action, desired


def _atomic_write_json(target: Path, data: dict[str, Any]) -> None:
    """Validate then atomically replace ``target`` with ``data``."""
    payload = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    json.loads(payload)  # re-parse guard before touching the real file
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + f".tmp.{os.getpid()}")
    tmp.write_text(payload, encoding="utf-8")
    os.replace(tmp, target)


def _backup(target: Path) -> Path | None:
    """Copy ``.mcp.json`` → ``.mcp.<timestamp>.bak`` (only if it exists)."""
    if not target.is_file():
        return None
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    bak = target.with_name(f"{target.stem}.{ts}.bak")
    shutil.copy2(target, bak)
    return bak


# ---------------------------------------------------------------------------
# marvis mcp register
# ---------------------------------------------------------------------------


@mcp_app.command("register")
def register_cmd(
    config: str | None = typer.Option(
        None, "--config", help="Target .mcp.json (default <cwd>/.mcp.json)."
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Print the entry + diff without writing."
    ),
    json_out: bool = typer.Option(False, "--json", help="Emit pure JSON to stdout."),
) -> None:
    """Merge the PiR MCP server entry into the target .mcp.json (idempotent)."""
    target = _resolve_target(config)
    current = _load_config(target)
    merged, action, entry = _merge_pir(current)

    will_change = action in ("add", "replace")

    backup_path: str | None = None
    if dry_run:
        status = "dry-run"
    elif not will_change:
        status = "already-registered"
    else:
        bak = _backup(target)
        backup_path = str(bak) if bak else None
        _atomic_write_json(target, merged)
        status = "registered"

    result = {
        "status": status,
        "target": str(target),
        "action": action,
        "entry": entry,
        "backup": backup_path,
        # Surface the other servers we preserved (names only) for transparency.
        "other_servers": sorted(
            k for k in (current.get("mcpServers") or {}) if k != _SERVER_KEY
        ),
    }

    # Anonymous telemetry: whether the PiR server ended up registered (a BOOL,
    # never the path/entry). Fail-silent + gated inside emit().
    if not dry_run:
        try:
            from core.telemetry import client as _telemetry

            _telemetry.emit(
                "mcp_registered", {"registered": status == "registered" or action == "skip-present"}
            )
        except Exception:  # noqa: BLE001 — telemetry never affects the command
            pass

    def _render(r: dict[str, Any]) -> None:
        if r["status"] == "already-registered":
            console.print("[green]pir already registered, no changes[/]")
        elif r["status"] == "dry-run":
            console.print("[yellow]dry-run — nothing written[/]")
        else:
            console.print(f"[green]pir registered[/] → {r['target']}")
        mark = {"add": "[green]+[/]", "replace": "[yellow]~[/]"}.get(r["action"], "[dim]=[/]")
        console.print(f"  {mark} mcpServers.pir ({r['action']})")
        console.print(f"    command: {r['entry']['command']}")
        console.print(f"    args:    {r['entry']['args']}")
        if r["other_servers"]:
            console.print(f"  preserved: {', '.join(r['other_servers'])}")
        if r["backup"]:
            console.print(f"  backup → {r['backup']}")

    emit(result, json_out=json_out, render=_render)


# ---------------------------------------------------------------------------
# marvis mcp status — REAL tools/list round-trip
# ---------------------------------------------------------------------------


def _registered_entry(target: Path) -> dict[str, Any] | None:
    """Return the ``pir`` entry from the target ``.mcp.json``, or ``None``."""
    config = _load_config(target)
    servers = config.get("mcpServers")
    if isinstance(servers, dict):
        entry = servers.get(_SERVER_KEY)
        if isinstance(entry, dict):
            return entry
    return None


def _stdio_tools_list(
    command: str, args: list[str], *, timeout: float = 20.0
) -> tuple[bool, int, str | None]:
    """Spawn the configured server over stdio and run initialize → tools/list.

    Returns ``(responds, tool_count, error)``. This is a dependency-free MCP
    handshake (line-delimited JSON-RPC over stdio): we do NOT rely on the ``mcp``
    SDK being importable in the CLI environment, and we spawn the EXACT
    ``command``+``args`` written in ``.mcp.json`` so a wrong interpreter path
    surfaces here as ENOENT / no response — the "registered but dead" case.
    """
    init_req = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "marvis-mcp-status", "version": "1.0.0"},
        },
    }
    initialized = {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}
    list_req = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
    stdin_blob = (
        json.dumps(init_req) + "\n"
        + json.dumps(initialized) + "\n"
        + json.dumps(list_req) + "\n"
    )

    try:
        proc = subprocess.run(
            [command, *args],
            input=stdin_blob,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError:
        return False, 0, f"interpreter not found: {command}"
    except subprocess.TimeoutExpired:
        return False, 0, "server did not respond within timeout"
    except OSError as exc:  # noqa: BLE001 — spawn failure is the signal we want
        return False, 0, f"spawn failed: {exc}"

    # Parse the line-delimited JSON-RPC responses; find the tools/list reply (id=2).
    tool_count: int | None = None
    for line in (proc.stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        if msg.get("id") == 2 and "result" in msg:
            tools = (msg.get("result") or {}).get("tools")
            if isinstance(tools, list):
                tool_count = len(tools)
                break

    if tool_count is None:
        snippet = (proc.stderr or "").strip().splitlines()
        err = snippet[-1] if snippet else "no tools/list response from server"
        return False, 0, err
    return True, tool_count, None


@mcp_app.command("status")
def status_cmd(
    config: str | None = typer.Option(
        None, "--config", help="Target .mcp.json (default <cwd>/.mcp.json)."
    ),
    no_probe: bool = typer.Option(
        False, "--no-probe", help="Only read .mcp.json; skip the live tools/list round-trip."
    ),
    timeout: float = typer.Option(
        20.0, "--timeout", help="Seconds to wait for the server tools/list reply."
    ),
    json_out: bool = typer.Option(False, "--json", help="Emit pure JSON to stdout."),
) -> None:
    """Report whether pir is registered AND responds (real tools/list round-trip)."""
    target = _resolve_target(config)
    entry = _registered_entry(target)
    registered = entry is not None

    responds = False
    tool_count = 0
    probe_error: str | None = None
    if registered and not no_probe:
        command = entry.get("command") or ""
        args = entry.get("args") or []
        if not isinstance(args, list):
            # A string `args` (or a `command` carrying the whole invocation) is the
            # documented broken shape — report it instead of trying to spawn it.
            responds, tool_count, probe_error = (
                False,
                0,
                "malformed entry: 'args' must be an array (got a string) — re-run 'marvis mcp register'",
            )
        else:
            responds, tool_count, probe_error = _stdio_tools_list(
                str(command), [str(a) for a in args], timeout=timeout
            )

    connected = registered and (no_probe or responds)

    result = {
        "target": str(target),
        "registered": registered,
        "entry": entry,
        "probed": registered and not no_probe,
        "responds": responds,
        "tool_count": tool_count,
        "error": probe_error,
        "connected": connected,
    }

    def _render(r: dict[str, Any]) -> None:
        from rich.table import Table

        t = Table(title="marvis mcp status", show_header=True)
        t.add_column("check")
        t.add_column("value")
        t.add_row("target", r["target"])
        t.add_row(
            "registered", "[green]yes[/]" if r["registered"] else "[red]no[/]"
        )
        if r["entry"]:
            t.add_row("command", str(r["entry"].get("command")))
            t.add_row("args", str(r["entry"].get("args")))
        if r["probed"]:
            t.add_row(
                "responds", "[green]yes[/]" if r["responds"] else "[red]no[/]"
            )
            t.add_row("tools", str(r["tool_count"]))
        elif r["registered"]:
            t.add_row("responds", "[dim]not probed[/]")
        console.print(t)
        if r["error"]:
            err_console.print(f"[red]{r['error']}[/red]")
        if r["connected"]:
            console.print("[green]connected[/]")
        elif not r["registered"]:
            console.print("[yellow]not registered — run 'marvis mcp register'[/]")
        else:
            console.print("[red]registered but not responding[/]")

    emit(result, json_out=json_out, render=_render)
