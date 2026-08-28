# v1.0.0 - 2026-05-27 - S2 F2: `marvis hooks` — ship the governance hooks (de-hardcoded)
"""``marvis hooks`` — install / uninstall / status for the MarvisX governance hooks.

Registered onto the SAME Typer ``app`` as ``marvis init`` (one entrypoint) via the
``register(app)`` pattern, exactly like ``marvis_runtime``.

What it does (plan S2 F2 §C):

1. Resolve target ``settings.json`` (default ``<cwd>/.claude/settings.json``).
2. **Backup** the target → ``settings.<timestamp>.bak`` (always, before any write).
3. Copy ONLY the known Marvis hook scripts (package data in
   ``core/scripts/install_hooks/``) into ``<target>/.claude/hooks/``. Hash-compare:
   identical = skip, different = replace ONLY a *known* Marvis script by name.
4. **Identity-keyed merge** into ``settings.json.hooks.PreToolUse``: for each matcher
   (``Write|Edit|MultiEdit``, ``Bash``) append a Marvis hook entry ONLY if absent.
   Identity = **basename of the command path ∈ known Marvis set**, never a free
   string. NEVER replace ``hooks.PreToolUse`` wholesale — append inside the
   existing matcher array, preserving every non-Marvis entry (e.g. cozempic).
5. Atomic write: tmp + ``os.replace``, with ``json.loads`` validation of the result.
6. ``--dry-run`` prints the diff (added / skipped) without writing.

MCP is NOT installed here (that is ``marvis mcp register`` → ``.mcp.json``, a later
phase). This command only touches ``settings.json`` + the hook scripts.

Idempotent by construction: re-running on an already-configured settings produces
0 entry changes (only a fresh backup). The known-name identity key makes the merge
robust to per-user ``$CLAUDE_PROJECT_DIR`` expansion.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import typer

from core.cli._runtime_ctx import console, emit
from core.cli.marvis_governance import (
    DEFAULT_PROFILE,
    VALID_PROFILES,
    ensure_default_profile,
    read_profile,
    write_profile,
)

# ---------------------------------------------------------------------------
# Known Marvis hook set (the identity key for "is this entry mine?").
# ---------------------------------------------------------------------------

# Source of truth for the shippable scripts (package data).
#
# These scripts ship as ``core.scripts.install_hooks`` package-data and are
# copied out to the user's ``.claude/hooks/`` with ``shutil.copy2``, so we need
# a REAL filesystem path. ``importlib.resources.files`` resolves correctly from
# an installed wheel (where ``__file__`` walking is brittle), and for the normal
# wheel/source layout it returns a real directory. Fall back to the repo-relative
# path for an editable/source checkout (learning 9e527cfa).
def _resolve_install_hooks_dir() -> Path:
    try:
        import importlib.resources as _res

        candidate = Path(str(_res.files("core.scripts.install_hooks")))
        if candidate.is_dir():
            return candidate
    except (ModuleNotFoundError, FileNotFoundError, TypeError, AttributeError):
        pass
    return Path(__file__).resolve().parent.parent / "scripts" / "install_hooks"


_INSTALL_HOOKS_DIR = _resolve_install_hooks_dir()

# Scripts wired into settings.json, keyed by their PreToolUse matcher. The
# basenames here ALSO form the provenance identity set: an entry in the user's
# settings is "ours" iff os.path.basename(command) is in _KNOWN_SCRIPTS.
_MATCHER_SCRIPTS: dict[str, tuple[str, ...]] = {
    "Write|Edit|MultiEdit": ("enforce-worktree.sh",),
    "Bash": (
        "block-dangerous-bash.sh",
        "enforce-no-merge-main.sh",
        "block-db-direct-write.sh",
        "block-push-no-task.sh",
        "block-staging-to-prod.sh",
        "block-subtree-push.sh",
        "secret-scan.sh",
        "quality-gate.sh",
    ),
}

# All files copied into <target>/.claude/hooks/ (scripts wired into settings +
# their support files, which carry no settings entry):
#   - config.json / _config.sh : hook config + shared shell helpers
#   - safety_bridge.py         : the rule engine the .sh wrappers shell out to.
#     The wrappers resolve it CO-LOCATED first ($HOOK_DIR/safety_bridge.py), so
#     shipping it here makes the installed governance hooks SELF-CONTAINED on a
#     clean project — without it the wrappers fail-closed on a missing file
#     (denying for the wrong reason instead of running the real rule logic).
_SUPPORT_FILES: tuple[str, ...] = ("config.json", "_config.sh", "safety_bridge.py")

_KNOWN_SCRIPTS: frozenset[str] = frozenset(
    name for names in _MATCHER_SCRIPTS.values() for name in names
)
_COPY_FILES: tuple[str, ...] = tuple(sorted(_KNOWN_SCRIPTS)) + _SUPPORT_FILES

_CMD_PREFIX = "$CLAUDE_PROJECT_DIR/.claude/hooks/"

_PANEL_HOOKS = "Hooks"


def register(app: typer.Typer) -> None:
    """Attach the ``hooks`` command group onto an existing app."""
    app.add_typer(
        hooks_app,
        name="hooks",
        rich_help_panel=_PANEL_HOOKS,
        help="Install / uninstall / inspect the MarvisX governance hooks.",
    )


hooks_app = typer.Typer(add_completion=False, no_args_is_help=True)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_target(settings: str | None) -> Path:
    """Default = ``<cwd>/.claude/settings.json``; ``--settings`` overrides."""
    if settings:
        return Path(settings).expanduser()
    return Path.cwd() / ".claude" / "settings.json"


def _hooks_dir(target: Path) -> Path:
    return target.parent / "hooks"


def _command_for(script: str) -> str:
    return _CMD_PREFIX + script


def _basename_of(command: str) -> str:
    """Basename of a hook command path (strips a leading ``python3``/``bash`` shim)."""
    cmd = (command or "").strip()
    # commands are "$CLAUDE_PROJECT_DIR/.claude/hooks/x.sh" or "python3 .../x.py"
    last = cmd.split()[-1] if cmd else ""
    return os.path.basename(last)


def _is_marvis_entry(entry: dict[str, Any]) -> bool:
    return _basename_of(entry.get("command", "")) in _KNOWN_SCRIPTS


def _load_settings(target: Path) -> dict[str, Any]:
    if not target.is_file():
        return {}
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _copy_scripts(hooks_dir: Path, *, dry_run: bool) -> list[dict[str, str]]:
    """Copy package-data scripts into ``hooks_dir``. Returns per-file actions.

    Identical (by hash) → skip. Different → replace ONLY a known Marvis file by
    name; an unknown file with the same name is never touched (it isn't ours).
    """
    missing = [
        name for name in _COPY_FILES if not (_INSTALL_HOOKS_DIR / name).is_file()
    ]
    if missing:
        joined = ", ".join(sorted(missing))
        raise RuntimeError(f"mandatory hook package data missing: {joined}")

    actions: list[dict[str, str]] = []
    for name in _COPY_FILES:
        src = _INSTALL_HOOKS_DIR / name
        dst = hooks_dir / name
        if dst.is_file():
            if _sha256(src) == _sha256(dst):
                actions.append({"file": name, "action": "skip-identical"})
                continue
            # Differs: it is a KNOWN Marvis script by name → safe to replace.
            action = "replace"
        else:
            action = "create"
        if not dry_run:
            hooks_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            if name.endswith((".sh", ".py")):
                dst.chmod(0o755)
        actions.append({"file": name, "action": action})
    return actions


def _merge_entries(settings: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """Identity-keyed append of Marvis hook entries. Returns (new_settings, diff).

    Preserves every non-Marvis entry. Never rebuilds ``PreToolUse`` wholesale —
    it appends a Marvis entry into the existing matcher block ONLY if a sibling
    with the same known basename is not already present.
    """
    diff: list[dict[str, str]] = []
    # Deep-ish copy so we never mutate the caller's dict in place.
    new = json.loads(json.dumps(settings))
    hooks = new.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        hooks = {}
        new["hooks"] = hooks
    pretool = hooks.setdefault("PreToolUse", [])
    if not isinstance(pretool, list):
        pretool = []
        hooks["PreToolUse"] = pretool

    for matcher, scripts in _MATCHER_SCRIPTS.items():
        # Find the existing matcher block (exact match), else create one.
        block = next(
            (b for b in pretool if isinstance(b, dict) and b.get("matcher") == matcher),
            None,
        )
        if block is None:
            block = {"matcher": matcher, "hooks": []}
            pretool.append(block)
        block_hooks = block.setdefault("hooks", [])
        if not isinstance(block_hooks, list):
            block_hooks = []
            block["hooks"] = block_hooks

        present = {
            _basename_of(h.get("command", ""))
            for h in block_hooks
            if isinstance(h, dict)
        }
        for script in scripts:
            if script in present:
                diff.append({"matcher": matcher, "script": script, "action": "skip-present"})
                continue
            block_hooks.append({"type": "command", "command": _command_for(script)})
            diff.append({"matcher": matcher, "script": script, "action": "add"})

    return new, diff


def _atomic_write_json(target: Path, data: dict[str, Any]) -> None:
    """Validate then atomically replace ``target`` with ``data``."""
    payload = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    json.loads(payload)  # re-parse guard before we touch the real file
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + f".tmp.{os.getpid()}")
    tmp.write_text(payload, encoding="utf-8")
    os.replace(tmp, target)


def _backup(target: Path) -> Path | None:
    """Copy ``settings.json`` → ``settings.<timestamp>.bak`` (only if it exists)."""
    if not target.is_file():
        return None
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    bak = target.with_name(f"{target.stem}.{ts}.bak")
    shutil.copy2(target, bak)
    return bak


# ---------------------------------------------------------------------------
# marvis hooks install
# ---------------------------------------------------------------------------


@hooks_app.command("install")
def install_cmd(
    settings: str | None = typer.Option(
        None, "--settings", help="Target settings.json (default <cwd>/.claude/settings.json)."
    ),
    profile: str | None = typer.Option(
        None,
        "--profile",
        help="Governance profile to write (lite|strict). Defaults to lite only on first install.",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Print the diff without writing."
    ),
    json_out: bool = typer.Option(False, "--json", help="Emit pure JSON to stdout."),
) -> None:
    """Copy the governance hook scripts + merge their entries into settings.json."""
    if profile is not None and profile.strip().lower() not in VALID_PROFILES:
        raise typer.BadParameter(f"Unknown governance profile: {profile}")
    profile = profile.strip().lower() if profile is not None else None

    target = _resolve_target(settings)
    hooks_dir = _hooks_dir(target)

    current = _load_settings(target)
    merged, entry_diff = _merge_entries(current)
    script_actions = _copy_scripts(hooks_dir, dry_run=dry_run)
    current_profile, profile_source = read_profile(target)

    added = [d for d in entry_diff if d["action"] == "add"]
    changed_scripts = [a for a in script_actions if a["action"] in ("create", "replace")]
    profile_result: dict[str, Any]
    if dry_run:
        profile_result = {
            "profile": profile or current_profile,
            "source": profile_source,
            "action": "dry-run",
        }
        profile_changed = bool(profile and profile != current_profile)
    else:
        profile_result = (
            write_profile(target, profile, overwrite=True)
            if profile
            else ensure_default_profile(target, DEFAULT_PROFILE)
        )
        profile_changed = profile_result["action"] == "updated"
    will_change = bool(added) or bool(changed_scripts) or profile_changed

    backup_path: str | None = None
    if dry_run:
        status = "dry-run"
    elif not will_change:
        # Settings already configured AND scripts identical → no-op, no backup.
        status = "already-installed"
    else:
        bak = _backup(target)
        backup_path = str(bak) if bak else None
        _atomic_write_json(target, merged)
        status = "installed"

    result = {
        "status": status,
        "target": str(target),
        "hooks_dir": str(hooks_dir),
        "backup": backup_path,
        "entries": entry_diff,
        "scripts": script_actions,
        "profile": profile_result,
        "added": len(added),
        "skipped": len([d for d in entry_diff if d["action"] == "skip-present"]),
    }

    # Anonymous telemetry: how many hook entries are wired in (a COUNT, never the
    # names/paths). Fail-silent + gated inside emit(); a dry-run reports the count
    # that WOULD be present so the install-funnel signal stays consistent.
    if not dry_run:
        try:
            from core.telemetry import client as _telemetry

            present_count = len(
                [d for d in entry_diff if d["action"] in ("add", "skip-present")]
            )
            _telemetry.emit("hooks_installed", {"count": present_count})
        except Exception:  # noqa: BLE001 — telemetry never affects the command
            pass

    def _render(r: dict[str, Any]) -> None:
        if r["status"] == "already-installed":
            console.print("[green]already installed, no changes[/]")
        elif r["status"] == "dry-run":
            console.print("[yellow]dry-run — nothing written[/]")
        else:
            console.print(f"[green]hooks installed[/] → {r['target']}")
        for d in r["entries"]:
            mark = "[green]+[/]" if d["action"] == "add" else "[dim]=[/]"
            console.print(f"  {mark} {d['matcher']}: {d['script']} ({d['action']})")
        for a in r["scripts"]:
            console.print(f"  · {a['file']}: {a['action']}")
        console.print(
            f"  governance profile → {r['profile']['profile']} ({r['profile']['action']})"
        )
        if r["backup"]:
            console.print(f"  backup → {r['backup']}")

    emit(result, json_out=json_out, render=_render)


# ---------------------------------------------------------------------------
# marvis hooks uninstall
# ---------------------------------------------------------------------------


@hooks_app.command("uninstall")
def uninstall_cmd(
    settings: str | None = typer.Option(
        None, "--settings", help="Target settings.json (default <cwd>/.claude/settings.json)."
    ),
    json_out: bool = typer.Option(False, "--json", help="Emit pure JSON to stdout."),
) -> None:
    """Remove ONLY the MarvisX hook entries from settings.json (leaves the rest)."""
    target = _resolve_target(settings)
    current = _load_settings(target)

    removed: list[str] = []
    new = json.loads(json.dumps(current))
    pretool = (new.get("hooks") or {}).get("PreToolUse")
    if isinstance(pretool, list):
        for block in pretool:
            if not isinstance(block, dict):
                continue
            block_hooks = block.get("hooks")
            if not isinstance(block_hooks, list):
                continue
            kept = []
            for h in block_hooks:
                if isinstance(h, dict) and _is_marvis_entry(h):
                    removed.append(_basename_of(h.get("command", "")))
                else:
                    kept.append(h)
            block["hooks"] = kept
        # Drop now-empty matcher blocks we may have emptied.
        new["hooks"]["PreToolUse"] = [
            b for b in pretool if not (isinstance(b, dict) and b.get("hooks") == [])
        ]

    backup_path: str | None = None
    if removed:
        bak = _backup(target)
        backup_path = str(bak) if bak else None
        _atomic_write_json(target, new)
        status = "uninstalled"
    else:
        status = "nothing-to-remove"

    result = {
        "status": status,
        "target": str(target),
        "removed": removed,
        "backup": backup_path,
    }

    def _render(r: dict[str, Any]) -> None:
        if r["status"] == "nothing-to-remove":
            console.print("[yellow]no MarvisX hooks present[/]")
            return
        console.print(f"[green]removed {len(r['removed'])} hook entries[/] → {r['target']}")
        for name in r["removed"]:
            console.print(f"  [red]-[/] {name}")
        if r["backup"]:
            console.print(f"  backup → {r['backup']}")

    emit(result, json_out=json_out, render=_render)


# ---------------------------------------------------------------------------
# marvis hooks status
# ---------------------------------------------------------------------------


@hooks_app.command("status")
def status_cmd(
    settings: str | None = typer.Option(
        None, "--settings", help="Target settings.json (default <cwd>/.claude/settings.json)."
    ),
    json_out: bool = typer.Option(False, "--json", help="Emit pure JSON to stdout."),
) -> None:
    """Show which MarvisX hooks are present in the target settings + on disk."""
    target = _resolve_target(settings)
    hooks_dir = _hooks_dir(target)
    current = _load_settings(target)

    present: dict[str, list[str]] = {}
    pretool = (current.get("hooks") or {}).get("PreToolUse")
    if isinstance(pretool, list):
        for block in pretool:
            if not isinstance(block, dict):
                continue
            matcher = block.get("matcher", "")
            for h in block.get("hooks") or []:
                if isinstance(h, dict) and _is_marvis_entry(h):
                    present.setdefault(matcher, []).append(_basename_of(h.get("command", "")))

    expected = {
        matcher: list(scripts) for matcher, scripts in _MATCHER_SCRIPTS.items()
    }
    on_disk = {
        name: (hooks_dir / name).is_file() for name in _COPY_FILES
    }
    fully_installed = all(
        all(s in present.get(matcher, []) for s in scripts)
        for matcher, scripts in _MATCHER_SCRIPTS.items()
    ) and all(on_disk.values())

    result = {
        "target": str(target),
        "hooks_dir": str(hooks_dir),
        "settings_exists": target.is_file(),
        "present": present,
        "expected": expected,
        "scripts_on_disk": on_disk,
        "fully_installed": fully_installed,
    }

    def _render(r: dict[str, Any]) -> None:
        from rich.table import Table

        t = Table(title="marvis hooks status", show_header=True)
        t.add_column("matcher")
        t.add_column("script")
        t.add_column("in settings")
        t.add_column("on disk")
        for matcher, scripts in _MATCHER_SCRIPTS.items():
            for s in scripts:
                in_set = "[green]yes[/]" if s in r["present"].get(matcher, []) else "[red]no[/]"
                disk = "[green]yes[/]" if r["scripts_on_disk"].get(s) else "[red]no[/]"
                t.add_row(matcher, s, in_set, disk)
        console.print(t)
        console.print(
            "[green]fully installed[/]" if r["fully_installed"] else "[yellow]incomplete[/]"
        )

    emit(result, json_out=json_out, render=_render)
