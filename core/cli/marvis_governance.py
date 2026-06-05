"""``marvis governance`` profile control for installed hook dispatchers."""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import typer

from core.cli._runtime_ctx import console, emit

GOVERNANCE_FILENAME = "governance.json"
DEFAULT_PROFILE = "lite"
VALID_PROFILES = {"lite", "strict"}

RULES: tuple[dict[str, str], ...] = (
    {
        "key": "dangerous-bash",
        "axis": "safety",
        "matcher": "Bash",
        "script": "block-dangerous-bash.sh",
        "description": "Block destructive shell commands and force-push.",
    },
    {
        "key": "db-write",
        "axis": "safety",
        "matcher": "Bash",
        "script": "block-db-direct-write.sh",
        "description": "Block direct Marvis database writes.",
    },
    {
        "key": "staging-to-prod",
        "axis": "safety",
        "matcher": "Bash",
        "script": "block-staging-to-prod.sh",
        "description": "Block direct writes into production paths.",
    },
    {
        "key": "secret-scan",
        "axis": "safety",
        "matcher": "Bash",
        "script": "secret-scan.sh",
        "description": "Block commits with likely secrets in the staged diff.",
    },
    {
        "key": "worktree",
        "axis": "process",
        "matcher": "Write|Edit|MultiEdit",
        "script": "enforce-worktree.sh",
        "description": "Require code edits through a feature worktree.",
    },
    {
        "key": "bash-merge",
        "axis": "process",
        "matcher": "Bash",
        "script": "enforce-no-merge-main.sh",
        "description": "Require merges through Triage.",
    },
    {
        "key": "push-no-task",
        "axis": "process",
        "matcher": "Bash",
        "script": "block-push-no-task.sh",
        "description": "Require pushes to trace to a Marvis task branch.",
    },
    {
        "key": "subtree-push",
        "axis": "process",
        "matcher": "Bash",
        "script": "block-subtree-push.sh",
        "description": "Require deploy pushes to flow through approved merge.",
    },
    {
        "key": "quality-gate",
        "axis": "process",
        "matcher": "Bash",
        "script": "quality-gate.sh",
        "description": "Run pre-commit quality gates when applicable.",
    },
)

RULE_BY_SCRIPT = {rule["script"]: rule for rule in RULES}


def register(app: typer.Typer) -> None:
    app.add_typer(
        governance_app,
        name="governance",
        rich_help_panel="Hooks",
        help="Switch / inspect MarvisX governance hook profiles.",
    )


governance_app = typer.Typer(add_completion=False, no_args_is_help=True)


def resolve_settings(settings: str | None) -> Path:
    if settings:
        return Path(settings).expanduser()
    return Path.cwd() / ".claude" / "settings.json"


def governance_path(settings_path: Path) -> Path:
    return settings_path.parent / GOVERNANCE_FILENAME


def _load_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def read_profile(settings_path: Path) -> tuple[str, str]:
    path = governance_path(settings_path)
    data = _load_json(path)
    profile = str(data.get("profile") or "").strip().lower()
    if profile in VALID_PROFILES:
        return profile, str(path)
    if path.exists():
        return "strict", f"invalid:{path}"
    return DEFAULT_PROFILE, "default"


def write_profile(settings_path: Path, profile: str, *, overwrite: bool) -> dict[str, Any]:
    profile = profile.strip().lower()
    if profile not in VALID_PROFILES:
        raise typer.BadParameter(f"Unknown governance profile: {profile}")

    path = governance_path(settings_path)
    current_profile, source = read_profile(settings_path)
    if path.is_file() and current_profile == profile and source == str(path):
        return {
            "path": str(path),
            "profile": current_profile,
            "source": source,
            "action": "already-set",
        }
    if path.is_file() and not overwrite:
        return {
            "path": str(path),
            "profile": current_profile,
            "source": source,
            "action": "preserve-existing",
        }

    payload = {
        "version": 1,
        "profile": profile,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)

    return {"path": str(path), "profile": profile, "source": str(path), "action": "updated"}


def ensure_default_profile(settings_path: Path, profile: str = DEFAULT_PROFILE) -> dict[str, Any]:
    return write_profile(settings_path, profile, overwrite=False)


def _settings_entries(settings_path: Path) -> dict[str, set[str]]:
    data = _load_json(settings_path)
    present: dict[str, set[str]] = {}
    pretool = (data.get("hooks") or {}).get("PreToolUse")
    if not isinstance(pretool, list):
        return present
    for block in pretool:
        if not isinstance(block, dict):
            continue
        matcher = str(block.get("matcher") or "")
        hooks = block.get("hooks") or []
        if not isinstance(hooks, list):
            continue
        for hook in hooks:
            if not isinstance(hook, dict):
                continue
            command = str(hook.get("command") or "")
            basename = os.path.basename(command.split()[-1]) if command.strip() else ""
            if basename in RULE_BY_SCRIPT:
                present.setdefault(matcher, set()).add(basename)
    return present


def _rule_state(rule: dict[str, str], profile: str) -> tuple[str, str]:
    if rule["axis"] == "safety":
        return "block", "safety"
    if profile == "strict":
        return "block", "profile:strict"
    return "warn", "profile:lite"


def build_status(settings_path: Path) -> dict[str, Any]:
    profile, profile_source = read_profile(settings_path)
    present = _settings_entries(settings_path)

    rules: list[dict[str, Any]] = []
    for rule in RULES:
        state, source_layer = _rule_state(rule, profile)
        installed = rule["script"] in present.get(rule["matcher"], set())
        rules.append(
            {
                "key": rule["key"],
                "axis": rule["axis"],
                "state": state,
                "source_layer": source_layer,
                "matcher": rule["matcher"],
                "script": rule["script"],
                "installed": installed,
                "description": rule["description"],
            }
        )

    safety_rules = [rule for rule in rules if rule["axis"] == "safety"]
    safety_active = [
        rule for rule in safety_rules if rule["state"] == "block" and rule["installed"]
    ]

    return {
        "settings": str(settings_path),
        "governance": str(governance_path(settings_path)),
        "profile": profile,
        "profile_source": profile_source,
        "rules": rules,
        "safety": {
            "active": len(safety_active),
            "total": len(safety_rules),
            "label": f"safety {len(safety_active)}/{len(safety_rules)} active",
        },
    }


def _set_profile(
    profile: str,
    *,
    settings: str | None,
    json_out: bool,
) -> None:
    target = resolve_settings(settings)
    result = write_profile(target, profile, overwrite=True)
    result["status"] = result["action"]
    result["settings"] = str(target)

    def _render(r: dict[str, Any]) -> None:
        if r["status"] == "already-set":
            console.print(f"[green]governance already {r['profile']}[/] -> {r['path']}")
        else:
            console.print(f"[green]governance {r['profile']}[/] -> {r['path']}")

    emit(result, json_out=json_out, render=_render)


@governance_app.command("lite")
def lite_cmd(
    settings: str | None = typer.Option(
        None, "--settings", help="Target settings.json (default <cwd>/.claude/settings.json)."
    ),
    json_out: bool = typer.Option(False, "--json", help="Emit pure JSON to stdout."),
) -> None:
    """Use OSS-lite governance: safety blocks, process rules warn."""
    _set_profile("lite", settings=settings, json_out=json_out)


@governance_app.command("strict")
def strict_cmd(
    settings: str | None = typer.Option(
        None, "--settings", help="Target settings.json (default <cwd>/.claude/settings.json)."
    ),
    json_out: bool = typer.Option(False, "--json", help="Emit pure JSON to stdout."),
) -> None:
    """Use strict governance: safety and process rules block."""
    _set_profile("strict", settings=settings, json_out=json_out)


@governance_app.command("status")
def status_cmd(
    settings: str | None = typer.Option(
        None, "--settings", help="Target settings.json (default <cwd>/.claude/settings.json)."
    ),
    json_out: bool = typer.Option(False, "--json", help="Emit pure JSON to stdout."),
) -> None:
    """Show resolved governance rule state and source layer."""
    target = resolve_settings(settings)
    result = build_status(target)

    def _render(r: dict[str, Any]) -> None:
        from rich.table import Table

        t = Table(title=f"marvis governance status ({r['profile']})", show_header=True)
        t.add_column("rule")
        t.add_column("axis")
        t.add_column("state")
        t.add_column("source")
        t.add_column("installed")
        for rule in r["rules"]:
            state = "[green]block[/]" if rule["state"] == "block" else "[yellow]warn[/]"
            installed = "[green]yes[/]" if rule["installed"] else "[red]no[/]"
            t.add_row(
                rule["key"],
                rule["axis"],
                state,
                rule["source_layer"],
                installed,
            )
        console.print(t)
        console.print(r["safety"]["label"])

    emit(result, json_out=json_out, render=_render)
