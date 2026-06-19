# v1.1.0 - 2026-06-01 - C4-A: dual-register marvis (canonical) + pir (legacy) keys
"""``marvis mcp register`` / ``marvis mcp status`` — wire the Marvis MCP server into
the user's ``.mcp.json`` (merge) and verify it actually responds.

Registered onto the SAME Typer ``app`` as ``marvis init`` via ``register(app)``,
exactly like ``marvis_hooks`` / ``marvis_runtime``.

What ``register`` does:

1. Resolve target ``.mcp.json`` (default ``<cwd>/.mcp.json``).
2. **Merge the server under TWO keys** into ``mcpServers``: ``marvis`` (canonical)
   and ``pir`` (legacy alias). Both point to the SAME server module, so the host
   exposes both ``mcp__marvis__*`` and ``mcp__marvis__*`` during the rebrand
   deprecation window. Per key: absent → add; present-same → no-op;
   present-different → replace. Every OTHER server the user has is preserved
   untouched. The legacy ``pir`` key is dropped only at the end of the window (a
   later, separate change) — never a hard cutover here.
3. **Backup** the target → ``.mcp.<timestamp>.bak`` before any write.
4. Atomic write: tmp + ``os.replace`` with a ``json.loads`` validation guard.
5. ``--dry-run`` prints the entries + diff without writing.

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

Note: two keys → the host spawns the server process once PER key (two stdio
children). For an OSS single-user install this is harmless (SQLite WAL handles
the concurrent readers; the busy-timeout covers the rare concurrent write). It is
the inherent cost of exposing two prefixes from one ``.mcp.json`` and ends when
the legacy ``pir`` key is retired.

``marvis mcp status`` does a REAL ``tools/list`` round-trip: it spawns the
configured server (the exact ``command`` + ``args`` from ``.mcp.json``) as a stdio
subprocess and runs the MCP ``initialize`` → ``tools/list`` handshake. Reading the
file only tells you it is *registered*; the round-trip tells you it *responds* —
catching the "registered but dead interpreter" class.
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
# The MCP server we own in the user's .mcp.json. During the rebrand deprecation
# window we dual-register it under TWO keys: `marvis` (canonical) + `pir`
# (legacy alias). Both point to the SAME server module, so `mcp__marvis__*` and
# `mcp__marvis__*` both resolve. We touch ONLY these keys — every other server the
# user registered is preserved untouched. The legacy `pir` key is removed only
# at the end of the window (a later, separate change).
# ---------------------------------------------------------------------------

_CANONICAL_KEY = "marvis"
_LEGACY_KEY = "pir"
_SERVER_KEYS = (_CANONICAL_KEY, _LEGACY_KEY)  # canonical first
_SERVER_MODULE = "core.api.mcp.server"
_PANEL_MCP = "MCP"
_CLAUDE_PLUGIN_MARKETPLACE = "marvisx-oss"
_CLAUDE_PLUGIN_REPO = "emiliomartucci/marvisx-oss"
_CLAUDE_PLUGIN_PACKAGE = "marvis@marvisx-oss"
_CLAUDE_PLUGIN_MARKERS = (
    _CLAUDE_PLUGIN_PACKAGE,
    _CLAUDE_PLUGIN_REPO,
    _CLAUDE_PLUGIN_MARKETPLACE,
)


def register(app: typer.Typer) -> None:
    """Attach the ``mcp`` command group onto an existing app."""
    app.add_typer(
        mcp_app,
        name="mcp",
        rich_help_panel=_PANEL_MCP,
        help="Register / inspect the Marvis MCP server (marvis + legacy pir) in your .mcp.json.",
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


def _server_entry() -> dict[str, Any]:
    """The canonical server entry (identical for every key we register).

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


def _merge_servers(
    config: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, str], dict[str, Any]]:
    """Merge BOTH server keys into ``mcpServers``. Returns (new, actions, entry).

    ``actions`` maps each key in ``_SERVER_KEYS`` to ``add`` / ``replace`` /
    ``skip-present``. Never rebuilds ``mcpServers`` wholesale — every server that
    is not one of ours is preserved verbatim.
    """
    # Deep copy so we never mutate the caller's dict.
    new = json.loads(json.dumps(config))
    servers = new.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        servers = {}
        new["mcpServers"] = servers

    desired = _server_entry()
    actions: dict[str, str] = {}
    for key in _SERVER_KEYS:
        existing = servers.get(key)
        if existing is None:
            servers[key] = desired
            actions[key] = "add"
        elif existing == desired:
            actions[key] = "skip-present"
        else:
            servers[key] = desired
            actions[key] = "replace"
    return new, actions, desired


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


def _claude_config_dir() -> Path:
    """Best-effort Claude Code config root."""
    configured = os.environ.get("CLAUDE_CONFIG_DIR")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".claude"


def _contains_marker(value: Any, markers: tuple[str, ...] = _CLAUDE_PLUGIN_MARKERS) -> bool:
    """Return whether a parsed JSON-ish value mentions the Marvis plugin identity."""
    if isinstance(value, str):
        lowered = value.lower()
        return any(marker in lowered for marker in markers)
    if isinstance(value, dict):
        return any(
            _contains_marker(k, markers) or _contains_marker(v, markers)
            for k, v in value.items()
        )
    if isinstance(value, list):
        return any(_contains_marker(v, markers) for v in value)
    return False


def _json_file_contains_marker(path: Path) -> bool:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — unreadable config means "not detected"
        return False
    return _contains_marker(data)


def _detect_claude_plugin() -> dict[str, Any]:
    """Best-effort detector for the Claude Code Marvis plugin.

    Claude Code's plugin config has moved while the feature has evolved, so the
    detector intentionally accepts a few low-risk signals: an installed plugin
    directory, the known marketplace registry, or a JSON config mentioning the
    ``marvis@marvisx-oss`` package/repo. Detection only changes our default from
    "write .mcp.json" to "offer/skip"; ``--force`` still allows the legacy route.
    """
    root = _claude_config_dir()
    plugin_root = root / "plugins"
    sources: list[str] = []

    direct_candidates = (
        plugin_root / "marvis",
        plugin_root / _CLAUDE_PLUGIN_PACKAGE,
        plugin_root / _CLAUDE_PLUGIN_MARKETPLACE,
        plugin_root / "marketplaces" / _CLAUDE_PLUGIN_MARKETPLACE,
        plugin_root / "marketplaces" / _CLAUDE_PLUGIN_MARKETPLACE / "marvis",
    )
    for candidate in direct_candidates:
        if candidate.exists():
            sources.append(str(candidate))

    json_candidates = (
        plugin_root / "known_marketplaces.json",
        plugin_root / "installed_plugins.json",
        plugin_root / "plugins.json",
        root / "plugins.json",
        root / "settings.json",
        Path.home() / ".claude.json",
    )
    for candidate in json_candidates:
        if candidate.is_file() and _json_file_contains_marker(candidate):
            sources.append(str(candidate))

    if plugin_root.exists():
        try:
            scanned = 0
            for path in plugin_root.rglob("*"):
                scanned += 1
                if scanned > 500:
                    break
                rel = str(path.relative_to(plugin_root)).lower()
                if _CLAUDE_PLUGIN_MARKETPLACE in rel or _CLAUDE_PLUGIN_PACKAGE in rel:
                    sources.append(str(path))
                    break
        except Exception:  # noqa: BLE001 — best-effort only
            pass

    unique_sources = sorted(dict.fromkeys(sources))
    return {
        "detected": bool(unique_sources),
        "package": _CLAUDE_PLUGIN_PACKAGE,
        "marketplace": _CLAUDE_PLUGIN_REPO,
        "sources": unique_sources[:5],
    }


# ---------------------------------------------------------------------------
# marvis mcp register
# ---------------------------------------------------------------------------


def _register_cmd_impl(
    *,
    config: str | None,
    dry_run: bool,
    json_out: bool,
    force: bool,
) -> None:
    """Merge the Marvis MCP server (marvis + legacy pir) into the target .mcp.json."""
    target = _resolve_target(config)
    current = _load_config(target)
    merged, actions, entry = _merge_servers(current)
    plugin = _detect_claude_plugin()

    will_change = any(a in ("add", "replace") for a in actions.values())
    skip_for_plugin = False
    if plugin["detected"] and not force and not dry_run:
        if json_out or not sys.stdin.isatty():
            skip_for_plugin = True
        else:
            skip_for_plugin = typer.confirm(
                (
                    "Marvis Claude Code plugin detected; skip writing .mcp.json "
                    "so the MCP server is not registered twice?"
                ),
                default=True,
            )

    backup_path: str | None = None
    if dry_run:
        status = "dry-run"
    elif skip_for_plugin:
        status = "skipped-plugin-detected"
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
        "keys": list(_SERVER_KEYS),
        "actions": actions,
        "entry": entry,
        "backup": backup_path,
        "claude_plugin": plugin,
        "force": force,
        # Surface the other servers we preserved (names only) for transparency.
        "other_servers": sorted(
            k for k in (current.get("mcpServers") or {}) if k not in _SERVER_KEYS
        ),
    }

    # Anonymous telemetry: whether the Marvis server ended up registered (a BOOL,
    # never the path/entry). Fail-silent + gated inside emit().
    if not dry_run:
        try:
            from core.telemetry import client as _telemetry

            registered_ok = status == "registered" or all(
                a == "skip-present" for a in actions.values()
            )
            _telemetry.emit("mcp_registered", {"registered": registered_ok})
        except Exception:  # noqa: BLE001 — telemetry never affects the command
            pass

    def _render(r: dict[str, Any]) -> None:
        if r["status"] == "already-registered":
            console.print("[green]marvis + pir already registered, no changes[/]")
        elif r["status"] == "dry-run":
            console.print("[yellow]dry-run — nothing written[/]")
        elif r["status"] == "skipped-plugin-detected":
            console.print(
                "[yellow]Marvis Claude Code plugin detected; skipped .mcp.json write[/]"
            )
            console.print("  plugin already provides the marvis MCP server")
            console.print("  use --force only if you are not using the plugin route")
        else:
            console.print(f"[green]marvis + pir registered[/] → {r['target']}")
        for key in r["keys"]:
            act = r["actions"].get(key, "skip-present")
            mark = {"add": "[green]+[/]", "replace": "[yellow]~[/]"}.get(act, "[dim]=[/]")
            console.print(f"  {mark} mcpServers.{key} ({act})")
        console.print(f"    command: {r['entry']['command']}")
        console.print(f"    args:    {r['entry']['args']}")
        if r["claude_plugin"]["detected"]:
            console.print(
                "  plugin: "
                f"{r['claude_plugin']['package']} detected "
                f"({len(r['claude_plugin']['sources'])} signal(s))"
            )
        if r["other_servers"]:
            console.print(f"  preserved: {', '.join(r['other_servers'])}")
        if r["backup"]:
            console.print(f"  backup → {r['backup']}")

    emit(result, json_out=json_out, render=_render)


@mcp_app.command("register")
def register_cmd(
    config: str | None = typer.Option(
        None, "--config", help="Target .mcp.json (default <cwd>/.mcp.json)."
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Print the entries + diff without writing."
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="Write .mcp.json even when the Claude Code plugin is detected.",
    ),
    json_out: bool = typer.Option(False, "--json", help="Emit pure JSON to stdout."),
) -> None:
    """Merge the Marvis MCP server (marvis + legacy pir) into the target .mcp.json."""
    _register_cmd_impl(config=config, dry_run=dry_run, json_out=json_out, force=force)


@mcp_app.command("install")
def install_cmd(
    config: str | None = typer.Option(
        None, "--config", help="Target .mcp.json (default <cwd>/.mcp.json)."
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Print the entries + diff without writing."
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="Write .mcp.json even when the Claude Code plugin is detected.",
    ),
    json_out: bool = typer.Option(False, "--json", help="Emit pure JSON to stdout."),
) -> None:
    """Alias for ``marvis mcp register``."""
    _register_cmd_impl(config=config, dry_run=dry_run, json_out=json_out, force=force)


# ---------------------------------------------------------------------------
# marvis mcp status — REAL tools/list round-trip
# ---------------------------------------------------------------------------


def _registered_keys(target: Path) -> list[str]:
    """Return which of our keys (``marvis`` / ``pir``) are present, canonical first."""
    config = _load_config(target)
    servers = config.get("mcpServers")
    if not isinstance(servers, dict):
        return []
    return [k for k in _SERVER_KEYS if isinstance(servers.get(k), dict)]


def _registered_entry(target: Path) -> dict[str, Any] | None:
    """Return the canonical (``marvis``) entry if present, else the legacy (``pir``).

    The two entries are identical, so probing whichever exists is equivalent.
    """
    config = _load_config(target)
    servers = config.get("mcpServers")
    if isinstance(servers, dict):
        for key in _SERVER_KEYS:
            entry = servers.get(key)
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
    """Report whether the server is registered AND responds (real tools/list round-trip)."""
    target = _resolve_target(config)
    keys = _registered_keys(target)
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
        "registered_keys": keys,
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
        if r["registered_keys"]:
            t.add_row("keys", ", ".join(r["registered_keys"]))
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
