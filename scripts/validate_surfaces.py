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
# The proofs a deployable local GUI owes, fixed here rather than read from the
# manifest: a gate that lets the audited file decide what it is audited against
# only certifies whatever it was handed.
REQUIRED_DESKTOP_PREREQUISITES = frozenset(
    {"local_gui_characterization", "desktop_host_contract", "perimeter_gate"}
)
# What a shipping local GUI must be true of, read from the files that do the
# shipping. `deployable: true` is a claim about the release, so it is checked
# against the release.
RELEASE_WORKFLOW = Path(".github/workflows/release.yml")
PYPROJECT = Path("pyproject.toml")
ADR_ACCEPTED_RE = re.compile(r"^status:\s*accepted\s*$", re.IGNORECASE | re.MULTILINE)


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

    The keys come from REQUIRED_DESKTOP_PREREQUISITES, not from the manifest, and
    each value must name a file that exists in the tree. Before this, `unproven`
    was computed from whatever mapping the manifest happened to carry: replacing
    it with a single evidenced key of any name passed.
    """
    errors: list[str] = []
    prereqs = desktop.get("prerequisites")
    if not isinstance(prereqs, dict):
        return ["desktop-ui: prerequisites must be a mapping of proof keys to evidence"]

    declared_keys = set(prereqs)
    missing = REQUIRED_DESKTOP_PREREQUISITES - declared_keys
    if missing:
        errors.append("desktop-ui: prerequisites missing required keys: " + ", ".join(sorted(missing)))
    unknown = declared_keys - REQUIRED_DESKTOP_PREREQUISITES
    if unknown:
        errors.append("desktop-ui: prerequisites declare unknown keys: " + ", ".join(sorted(unknown)))

    errors.extend(desktop_shell_errors(root, desktop))

    if desktop.get("deployable") is not True:
        return errors

    for key in sorted(REQUIRED_DESKTOP_PREREQUISITES & declared_keys):
        evidence = prereqs[key]
        if not isinstance(evidence, str) or not evidence.strip():
            errors.append(f"desktop-ui: deployable=true but {key} has no proof")
        elif not (root / evidence.strip()).exists():
            errors.append(
                f"desktop-ui: {key} points at {evidence.strip()}, which does not exist in the tree"
            )

    errors.extend(shipping_claim_errors(root, desktop))
    return errors


def desktop_shell_errors(root: Path, desktop: dict) -> list[str]:
    """Packaging the GUI as an installed app waits on an accepted ADR."""
    shell = desktop.get("desktop_shell")
    if not isinstance(shell, dict):
        return ["desktop-ui: desktop_shell must be a mapping with deployable and decision_record"]

    errors: list[str] = []
    record = shell.get("decision_record")
    if not isinstance(record, str) or not (root / record).is_file():
        errors.append("desktop-ui: desktop_shell.decision_record must name an existing ADR")
        return errors

    if shell.get("deployable") is True:
        text = (root / record).read_text(encoding="utf-8")
        if not ADR_ACCEPTED_RE.search(text):
            errors.append(
                f"desktop-ui: desktop_shell.deployable=true while {record} is not accepted — "
                "no shell technology has been chosen"
            )
    return errors


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
    if not built_from or not (root / built_from).is_dir():
        errors.append(f"desktop-ui: ships_as.built_from {built_from!r} is not a directory in the tree")
    if not bundled_at:
        errors.append("desktop-ui: ships_as.bundled_at must name the bundled location")

    try:
        workflow = (root / RELEASE_WORKFLOW).read_text(encoding="utf-8")
    except OSError as exc:
        return errors + [f"cannot read {RELEASE_WORKFLOW}: {exc}"]
    if built_from and built_from not in workflow:
        errors.append(
            f"desktop-ui: deployable=true but {RELEASE_WORKFLOW} never builds {built_from}"
        )
    if bundled_at and bundled_at not in workflow:
        errors.append(
            f"desktop-ui: deployable=true but {RELEASE_WORKFLOW} never produces {bundled_at}"
        )

    try:
        pyproject = (root / PYPROJECT).read_text(encoding="utf-8")
    except OSError as exc:
        return errors + [f"cannot read {PYPROJECT}: {exc}"]
    # The wheel ships the export as package data; without it the GUI is built
    # in CI and then dropped on the floor. Match the recursive glob, not the
    # bare name: pyproject also mentions the directory in prose, and a substring
    # test reads a comment as a shipping declaration.
    bundled_package_data = bundled_at.rsplit("/", 1)[-1] if bundled_at else ""
    if bundled_package_data and f"{bundled_package_data}/**/*" not in pyproject:
        errors.append(
            f"desktop-ui: deployable=true but {PYPROJECT} does not ship {bundled_package_data}"
        )
    return errors


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
