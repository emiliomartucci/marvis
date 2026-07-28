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
REQUIRED_FIELDS = ("surface_id", "owner_project", "owner_repo", "contract_version", "description")
# Hostnames owned by other products in the portfolio; no manifest here may claim them.
FOREIGN_HOSTNAMES = {
    "console.justaskmarvis.com",
    "emilio.cloud.justaskmarvis.com",
    "cloud.justaskmarvis.com",
    "t.justaskmarvis.com",
    "app.justaskmarvis.com",
    "api.justaskmarvis.com",
}


def pyproject_distribution_name(root: Path) -> str | None:
    text = (root / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'^name\s*=\s*"([^"]+)"', text, re.MULTILINE)
    return match.group(1) if match else None


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
        for field in REQUIRED_FIELDS:
            if not doc.get(field) and doc.get(field) != 0:
                errors.append(f"{where}: missing required field {field}")
        sid = doc.get("surface_id")
        if sid in manifests:
            errors.append(f"{where}: duplicate surface_id {sid}")
        manifests[sid] = doc
        if doc.get("owner_project") and doc["owner_project"] != OWNER_PROJECT:
            errors.append(f"{where}: owner_project {doc['owner_project']} != {OWNER_PROJECT}")
        if doc.get("owner_repo") and doc["owner_repo"] != OWNER_REPO:
            errors.append(f"{where}: owner_repo {doc['owner_repo']} != {OWNER_REPO}")
        if "contract_version" in doc and not isinstance(doc["contract_version"], int):
            errors.append(f"{where}: contract_version must be an integer")
        for host in doc.get("allowed_hostnames") or []:
            if host in FOREIGN_HOSTNAMES:
                errors.append(f"{where}: hostname {host} is owned by another product")

    cli = manifests.get("marvis-cli")
    if cli is None:
        errors.append("marvis-cli manifest missing — the shipped wheel would be unregistered")
    else:
        real_name = pyproject_distribution_name(root)
        declared = cli.get("distribution_name")
        if real_name and declared and real_name != declared:
            errors.append(
                f"marvis-cli: distribution_name {declared} != pyproject.toml name {real_name}"
            )

    desktop = manifests.get("desktop-ui")
    if desktop is None:
        errors.append("desktop-ui manifest missing — the future GUI surface must stay declared")
    elif desktop.get("deployable") is True:
        prereqs = desktop.get("prerequisites") or {}
        unproven = [key for key, value in prereqs.items() if value in (None, "")]
        if not prereqs:
            errors.append("desktop-ui: deployable=true with no prerequisites declared")
        if unproven:
            errors.append(
                "desktop-ui: deployable=true but prerequisites without proof: " + ", ".join(unproven)
            )

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
        "surface registry valid: marvis-cli matches pyproject.toml, "
        "desktop-ui is fail-closed, engine pin ok"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
