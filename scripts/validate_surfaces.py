#!/usr/bin/env python3
"""Fail-closed surface registry validator for the marvis product repo.

U1 portfolio registry / U4 consumer slice of the separation goal
(marvisx:docs/goals/2026-07-24-separate-surfaces.md). Runs in CI before any
build or release step; a non-empty error list means nothing may ship.

Incident lineage: a generic Console artifact once shipped the wrong UI contract
to another product's surface behind a green CI (marvisx postmortem 2026-07-24).
This registry exists so an owner/target mismatch fails BEFORE any artifact is
built.
"""

from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path

import yaml

OWNER_PROJECT = "marvis"
OWNER_REPO = "emiliomartucci/marvis"
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
# Split by type: the first version used one tuple and a `!= 0` escape for the
# integer field, which made `owner_project: 0` count as present and then skipped
# the ownership comparison behind a truthiness guard.
REQUIRED_STRING_FIELDS = ("surface_id", "owner_project", "owner_repo", "description")
REQUIRED_INT_FIELDS = ("contract_version",)
# Hostnames owned by other products in the portfolio; no manifest here may claim them.
FOREIGN_HOSTNAMES = {
    "console.justaskmarvis.com",
    "emilio.cloud.justaskmarvis.com",
    "cloud.justaskmarvis.com",
    "t.justaskmarvis.com",
    "app.justaskmarvis.com",
    "api.justaskmarvis.com",
}
# The proofs a deployable local GUI owes, and the artifact each one means.
# Fixed here rather than read from the manifest, keys AND values: a gate that
# lets the audited file decide what it is audited against certifies whatever it
# was handed, and checking only that the path exists lets any existing file —
# `pyproject.toml`, or `.` — stand in for the real evidence.
REQUIRED_DESKTOP_PREREQUISITES = {
    "local_gui_characterization": "core/cli/tests/test_marvis_console_characterization.py",
    "desktop_host_contract": "contracts/desktop-host.yaml",
    "perimeter_gate": "scripts/validate_local_surfaces.py",
}
DESKTOP_HOST_CONTRACT = Path("contracts/desktop-host.yaml")
# Where the local GUI is built and bundled. Pinned for the same reason the
# prerequisite paths are: a substring check let the manifest shorten the claim —
# `built_from: apps` and `bundled_at: console_dist` both matched the workflow
# and the package data while naming neither the source nor the bundle.
EXPECTED_SHIPS_AS = {
    "built_from": "apps/desktop-ui",
    "bundled_at": "core/api/console_dist",
}
# The one record that can authorise a desktop shell. Without pinning it, the
# manifest picks whichever accepted ADR happens to exist and authorises itself.
SHELL_DECISION_RECORD = "docs/decisions/desktop-shell-selection.md"
# What a shipping local GUI must be true of, read from the files that do the
# shipping. `deployable: true` is a claim about the release, so it is checked
# against the release.
RELEASE_WORKFLOW = Path(".github/workflows/release.yml")
PYPROJECT = Path("pyproject.toml")
MIN_SAFE_MCP_REQUIREMENT = "mcp>=1.28.1"
# Front matter only. A whole-document search matched `status: accepted` inside
# a fenced example or a migration note and read an open ADR as decided.
FRONT_MATTER_RE = re.compile(r"\A---\n(.*?)\n---\s*\n", re.DOTALL)


def pyproject_distribution_name(root: Path) -> str | None:
    text = (root / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'^name\s*=\s*"([^"]+)"', text, re.MULTILINE)
    return match.group(1) if match else None


def canonical_hostname(host: object) -> str:
    """DNS-equivalent form: case-insensitive, optional root dot removed.

    `Console.JustAskMarvis.com` and `console.justaskmarvis.com.` are the same
    host. A raw membership test read them as two strangers and let both through.
    """
    return str(host).strip().rstrip(".").lower()


def desktop_prerequisite_errors(root: Path, desktop: dict) -> list[str]:
    """The local GUI may only claim deployable against the fixed proof set.

    Keys and values both come from REQUIRED_DESKTOP_PREREQUISITES, not from the
    manifest. `unproven` used to be computed from whatever mapping the manifest
    carried, so a single evidenced key of any name passed; then only the key set
    was fixed, so any existing path — `pyproject.toml`, or `.` — could stand in
    for the proof it was supposed to name.
    """
    errors: list[str] = []
    prereqs = desktop.get("prerequisites")
    if not isinstance(prereqs, dict):
        return ["desktop-ui: prerequisites must be a mapping of proof keys to evidence"]

    declared_keys = set(prereqs)
    missing = set(REQUIRED_DESKTOP_PREREQUISITES) - declared_keys
    if missing:
        errors.append("desktop-ui: prerequisites missing required keys: " + ", ".join(sorted(missing)))
    unknown = declared_keys - set(REQUIRED_DESKTOP_PREREQUISITES)
    if unknown:
        errors.append("desktop-ui: prerequisites declare unknown keys: " + ", ".join(sorted(unknown)))

    errors.extend(desktop_shell_errors(root, desktop))

    if desktop.get("deployable") is not True:
        return errors

    for key in sorted(set(REQUIRED_DESKTOP_PREREQUISITES) & declared_keys):
        expected = REQUIRED_DESKTOP_PREREQUISITES[key]
        evidence = prereqs[key]
        if not isinstance(evidence, str) or evidence.strip() != expected:
            errors.append(
                f"desktop-ui: deployable=true but {key} must name {expected}, not {evidence!r}"
            )
        elif not (root / expected).is_file():
            errors.append(f"desktop-ui: {key} names {expected}, which is not a file in the tree")

    errors.extend(shipping_claim_errors(root, desktop))
    return errors


def desktop_shell_errors(root: Path, desktop: dict) -> list[str]:
    """Packaging the GUI as an installed app waits on an accepted ADR."""
    shell = desktop.get("desktop_shell")
    if not isinstance(shell, dict):
        return ["desktop-ui: desktop_shell must be a mapping with deployable and decision_record"]

    errors: list[str] = []
    record = shell.get("decision_record")
    if record != SHELL_DECISION_RECORD:
        return errors + [
            f"desktop-ui: desktop_shell.decision_record must be {SHELL_DECISION_RECORD}, not {record!r}"
        ]
    if not (root / record).is_file():
        return errors + [f"desktop-ui: {record} does not exist in the tree"]

    deployable = shell.get("deployable")
    # A malformed truthy value such as the string "true" is not `is True`, so the
    # identity check read it as undeployable and skipped the ADR gate entirely.
    if not isinstance(deployable, bool):
        return errors + ["desktop-ui: desktop_shell.deployable must be a boolean"]

    if not deployable:
        return errors

    if adr_status(root / record) != "accepted":
        errors.append(
            f"desktop-ui: desktop_shell.deployable=true while {record} is not accepted — "
            "no shell technology has been chosen"
        )

    # An accepted ADR decides WHICH shell, not that one exists. The flag is a
    # claim about a shipped artifact, so it is checked against one — the same
    # way the outer deployable flag is checked against its shipping path.
    # Nothing packages a desktop application today: the release builds a static
    # export and puts it in the wheel.
    errors.extend(shell_packaging_errors(root, shell))
    return errors


def shell_packaging_errors(root: Path, shell: dict) -> list[str]:
    """A deployable shell must name what builds it and what that produces."""
    packaged = shell.get("packaged_as")
    if not isinstance(packaged, dict):
        return [
            "desktop-ui: desktop_shell.deployable=true requires a packaged_as block "
            "naming built_by and artifact — no desktop application is packaged today"
        ]

    errors: list[str] = []
    built_by = str(packaged.get("built_by") or "").strip()
    artifact = str(packaged.get("artifact") or "").strip()
    if not built_by or not (root / built_by).exists():
        errors.append(
            f"desktop-ui: desktop_shell.packaged_as.built_by {built_by!r} does not exist in the tree"
        )
    if not artifact:
        errors.append("desktop-ui: desktop_shell.packaged_as.artifact must name what is produced")

    try:
        commands = release_run_commands(root)
    except OSError as exc:
        return errors + [f"cannot read {RELEASE_WORKFLOW}: {exc}"]
    if built_by and not any(built_by in line for line in commands):
        errors.append(
            f"desktop-ui: desktop_shell.deployable=true but {RELEASE_WORKFLOW} never runs {built_by}"
        )
    return errors


def adr_status(path: Path) -> str | None:
    """The `status` field of the ADR front matter, or None if there is none."""
    match = FRONT_MATTER_RE.match(path.read_text(encoding="utf-8"))
    if not match:
        return None
    try:
        front = yaml.safe_load(match.group(1))
    except yaml.YAMLError:
        return None
    if not isinstance(front, dict):
        return None
    status = front.get("status")
    return status.strip().lower() if isinstance(status, str) else None


def shipping_claim_errors(root: Path, desktop: dict) -> list[str]:
    """`deployable: true` must match what the release actually does.

    Anchored to the release workflow and the wheel packaging so the flag states
    a fact. The opposite drift — the GUI shipping while the manifest denied it —
    is what left this surface unregistered.
    """
    errors: list[str] = []
    ships = desktop.get("ships_as")
    if not isinstance(ships, dict):
        return ["desktop-ui: deployable=true requires a ships_as block"]

    built_from = str(ships.get("built_from") or "").strip()
    bundled_at = str(ships.get("bundled_at") or "").strip()
    for field, expected in EXPECTED_SHIPS_AS.items():
        declared = str(ships.get(field) or "").strip()
        if declared != expected:
            errors.append(f"desktop-ui: ships_as.{field} must be {expected}, not {declared!r}")
    if built_from and not (root / built_from).is_dir():
        errors.append(f"desktop-ui: ships_as.built_from {built_from!r} is not a directory in the tree")

    served_at = str(ships.get("served_at") or "").strip()
    # The route is fixed by the desktop host contract, which the launcher is
    # checked against in turn. Without this, the shipping claim could name any
    # path and stay green.
    try:
        host = yaml.safe_load((root / DESKTOP_HOST_CONTRACT).read_text(encoding="utf-8"))
    except OSError as exc:
        errors.append(f"cannot read {DESKTOP_HOST_CONTRACT}: {exc}")
    else:
        authoritative = ((host or {}).get("endpoint") or {}).get("ui_path")
        if served_at != authoritative:
            errors.append(
                f"desktop-ui: ships_as.served_at {served_at!r} != {DESKTOP_HOST_CONTRACT} "
                f"endpoint.ui_path {authoritative!r}"
            )

    try:
        workflow_steps = release_run_commands(root)
    except OSError as exc:
        return errors + [f"cannot read {RELEASE_WORKFLOW}: {exc}"]
    if built_from and not any(built_from in line for line in workflow_steps):
        errors.append(
            f"desktop-ui: deployable=true but {RELEASE_WORKFLOW} never builds {built_from}"
        )
    if bundled_at and not any(bundled_at in line for line in workflow_steps):
        errors.append(
            f"desktop-ui: deployable=true but {RELEASE_WORKFLOW} never produces {bundled_at}"
        )

    # The wheel ships the export as package data. Reading the raw text matched a
    # commented-out entry, which is how a setuptools setting is usually
    # disabled: parse the table instead.
    try:
        pyproject = tomllib.loads((root / PYPROJECT).read_text(encoding="utf-8"))
    except OSError as exc:
        return errors + [f"cannot read {PYPROJECT}: {exc}"]
    except tomllib.TOMLDecodeError as exc:
        return errors + [f"{PYPROJECT} is not valid TOML: {exc}"]

    package_data = (
        pyproject.get("tool", {}).get("setuptools", {}).get("package-data", {})
    )
    # Derived from the bundle path rather than matched loosely: core/api/console_dist
    # means the `core.api` package must ship globs under `console_dist/`. Scanning
    # every package's globs would accept the right glob under the wrong package.
    if bundled_at and "/" in bundled_at:
        package, _, directory = bundled_at.rpartition("/")
        package = package.replace("/", ".")
        globs = package_data.get(package) or []
        if not any(str(glob).startswith(f"{directory}/") for glob in globs):
            errors.append(
                f"desktop-ui: deployable=true but {PYPROJECT} package-data for {package!r} "
                f"does not ship {directory}"
            )
    return errors


def release_run_commands(root: Path) -> list[str]:
    """Active command lines of every `run:` step in the release workflow.

    Parsed, not grepped: a commented-out `# python scripts/validate_surfaces.py`
    still contains the script name, so a text search reported a gate that no
    longer executes.
    """
    workflow = yaml.safe_load((root / RELEASE_WORKFLOW).read_text(encoding="utf-8"))
    lines: list[str] = []
    for job in (workflow.get("jobs") or {}).values():
        for step in (job or {}).get("steps") or []:
            script = (step or {}).get("run")
            if not isinstance(script, str):
                continue
            lines.extend(
                line.strip()
                for line in script.split("\n")
                if line.strip() and not line.strip().startswith("#")
            )
    return lines


def validate(root: Path) -> list[str]:
    errors: list[str] = []
    surfaces_dir = root / "contracts" / "surfaces"
    if not surfaces_dir.is_dir():
        return [f"missing {surfaces_dir} — registry is absent"]

    manifests = {}
    for path in sorted(surfaces_dir.glob("*.yaml")):
        where = f"contracts/surfaces/{path.name}"
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(doc, dict):
            errors.append(f"{where}: not a mapping")
            continue
        for field in REQUIRED_STRING_FIELDS:
            value = doc.get(field)
            if not isinstance(value, str) or not value.strip():
                errors.append(f"{where}: {field} must be a non-empty string")
        for field in REQUIRED_INT_FIELDS:
            if not isinstance(doc.get(field), int) or isinstance(doc.get(field), bool):
                errors.append(f"{where}: {field} must be an integer")
        sid = doc.get("surface_id")
        if sid in manifests:
            errors.append(f"{where}: duplicate surface_id {sid}")
        manifests[sid] = doc
        # Compared unconditionally: guarding on truthiness let a falsy value
        # such as `owner_project: 0` skip the very check it should fail.
        if doc.get("owner_project") != OWNER_PROJECT:
            errors.append(f"{where}: owner_project {doc.get('owner_project')!r} != {OWNER_PROJECT}")
        if doc.get("owner_repo") != OWNER_REPO:
            errors.append(f"{where}: owner_repo {doc.get('owner_repo')!r} != {OWNER_REPO}")
        for host in doc.get("allowed_hostnames") or []:
            if canonical_hostname(host) in FOREIGN_HOSTNAMES:
                errors.append(f"{where}: hostname {host} is owned by another product")

    cli = manifests.get("marvis-cli")
    if cli is None:
        errors.append("marvis-cli manifest missing — the shipped wheel would be unregistered")
    else:
        # `distribution_name` is what ties this manifest to the wheel that
        # actually ships. Deleting it used to make the cross-check silent
        # instead of red, so the registry stayed green while identifying
        # nothing.
        real_name = pyproject_distribution_name(root)
        declared = cli.get("distribution_name")
        if not isinstance(declared, str) or not declared.strip():
            errors.append("marvis-cli: distribution_name must be a non-empty string")
        elif real_name is None:
            errors.append("marvis-cli: pyproject.toml declares no distribution name to compare")
        elif real_name != declared:
            errors.append(
                f"marvis-cli: distribution_name {declared} != pyproject.toml name {real_name}"
            )

    desktop = manifests.get("desktop-ui")
    if desktop is None:
        errors.append("desktop-ui manifest missing — the local GUI surface must stay declared")
    else:
        errors.extend(desktop_prerequisite_errors(root, desktop))

    pin_path = root / "contracts" / "engine-pin.yaml"
    try:
        pin = yaml.safe_load(pin_path.read_text(encoding="utf-8"))
    except OSError as exc:
        errors.append(f"cannot read contracts/engine-pin.yaml: {exc}")
    else:
        if not isinstance(pin, dict) or pin.get("engine") != "marvisx":
            errors.append("engine-pin: engine must be marvisx")
        if not isinstance((pin or {}).get("contract_version"), int):
            errors.append("engine-pin: contract_version must be an integer")
        if not SHA_RE.match(str((pin or {}).get("engine_ref", ""))):
            errors.append("engine-pin: engine_ref must be a 40-hex commit SHA")

    try:
        pyproject = tomllib.loads((root / PYPROJECT).read_text(encoding="utf-8"))
    except OSError as exc:
        errors.append(f"cannot read {PYPROJECT}: {exc}")
    except tomllib.TOMLDecodeError as exc:
        errors.append(f"{PYPROJECT} is not valid TOML: {exc}")
    else:
        dependencies = pyproject.get("project", {}).get("dependencies")
        if not isinstance(dependencies, list) or MIN_SAFE_MCP_REQUIREMENT not in dependencies:
            errors.append(
                "pyproject: MCP security floor must be exactly "
                f"{MIN_SAFE_MCP_REQUIREMENT} (CVE-2026-59950)"
            )

    return errors


def main() -> int:
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path.cwd()
    errors = validate(root)
    if errors:
        print("surface registry INVALID:", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        return 1
    print(
        "surface registry valid: marvis-cli matches pyproject.toml, the local GUI "
        "ships the way it declares, the desktop shell stays gated, engine pin ok"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
