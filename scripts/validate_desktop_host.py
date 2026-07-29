#!/usr/bin/env python3
"""Fail-closed gate for the desktop host contract (U8).

A contract that only lives in prose stops matching the product within weeks.
This checks contracts/desktop-host.yaml against the real launcher: the loopback
endpoint must be the one the launcher opens, and every declared capability must
resolve to a command the CLI actually registers.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml

CONTRACT = Path("contracts/desktop-host.yaml")
LAUNCHER = Path("core/cli/marvis_console.py")
PERIMETER = Path("apps/desktop-ui/surfaces.yaml")


def launcher_constants(source: str) -> dict[str, str]:
    """Endpoint constants as the launcher defines them."""
    found = {}
    for name in ("_HOST", "_PORT", "_UI_PATH"):
        match = re.search(rf'^{name} = "?([^"\n]+)"?$', source, re.MULTILINE)
        if match:
            found[name] = match.group(1)
    return found


def registered_commands(source: str) -> set[str]:
    """Commands the launcher attaches to the CLI, including sub-app verbs."""
    # Registration carries extra keyword arguments (help panels), so match the
    # command name without requiring the call to close straight after it.
    commands = set(re.findall(r'(?<!_)app\.command\(\s*"([^"]+)"', source))
    for sub in re.findall(r'add_typer\(\s*\w+,\s*\n?\s*name="([^"]+)"', source):
        commands.add(sub)
    # Verbs registered on a sub-app (autostart enable/disable/status).
    for group, verb in re.findall(r'(\w+)_app\.command\(\s*"([^"]+)"', source):
        commands.add(f"{group} {verb}")
    return commands


def validate(root: Path) -> list[str]:
    errors: list[str] = []
    contract = yaml.safe_load((root / CONTRACT).read_text(encoding="utf-8"))
    launcher = (root / LAUNCHER).read_text(encoding="utf-8")

    if contract.get("owner_project") != "marvis":
        errors.append("desktop-host: owner_project must be marvis")

    # 1. The endpoint must be the one the launcher really opens.
    endpoint = contract.get("endpoint") or {}
    constants = launcher_constants(launcher)
    expected = {
        "host": constants.get("_HOST"),
        "port": constants.get("_PORT"),
        "ui_path": constants.get("_UI_PATH"),
    }
    for key, real in expected.items():
        if real is None:
            errors.append(f"desktop-host: cannot read {key} from the launcher")
            continue
        declared = str(endpoint.get(key, ""))
        if declared != real:
            errors.append(f"desktop-host: endpoint {key} is {declared!r}, launcher uses {real!r}")
    if endpoint.get("host") not in {"127.0.0.1", "localhost"}:
        errors.append("desktop-host: the runtime must stay on loopback")

    # 2. Every capability must resolve to a command that exists.
    commands = registered_commands(launcher)
    capabilities = contract.get("capabilities") or {}
    if not capabilities:
        errors.append("desktop-host: no capabilities declared — refusing to pass")
    for name, spec in capabilities.items():
        command = (spec or {}).get("command")
        if not command:
            errors.append(f"desktop-host: capability {name} declares no command")
            continue
        root_command = command.split()[0]
        if command not in commands and root_command not in commands:
            errors.append(f"desktop-host: capability {name} maps to unknown command {command!r}")

    # 3. Forbidden rules must carry a reason; an empty rule is decoration.
    forbidden = contract.get("forbidden") or {}
    if not forbidden:
        errors.append("desktop-host: no forbidden rules declared — refusing to pass")
    for name, reason in forbidden.items():
        if not str(reason or "").strip():
            errors.append(f"desktop-host: forbidden rule {name} has no rationale")
    overlap = set(forbidden) & set(capabilities)
    if overlap:
        errors.append(f"desktop-host: declared both allowed and forbidden: {sorted(overlap)}")

    # 4. Permissions belong to the runtime, never to the shell.
    if (contract.get("permissions") or {}).get("owner") != "local-runtime":
        errors.append("desktop-host: permissions owner must be local-runtime")

    # 5. The shell selection stays open here (KTD4): this contract must not
    #    name or imply a technology.
    if contract.get("shell_selection") != "open":
        errors.append("desktop-host: shell_selection must stay open — the choice is a separate ADR")
    record = contract.get("shell_selection_record")
    if not record or not (root / record).is_file():
        errors.append("desktop-host: shell_selection_record must point at an existing document")

    # 6. The perimeter it serves must be the one the local product owns.
    if not (root / PERIMETER).is_file():
        errors.append(f"desktop-host: {PERIMETER} is missing — no perimeter to serve")

    return errors


def main() -> int:
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path.cwd()
    errors = validate(root)
    if errors:
        print("desktop host contract INVALID:", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        return 1
    print(
        "desktop host contract valid: endpoint and capabilities match the launcher, "
        "permissions stay with the runtime, shell choice still open"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
