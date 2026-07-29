#!/usr/bin/env python3
"""Fail-closed gate for the local GUI ownership perimeter (U7).

The perimeter is only meaningful if it matches the code. This reads LOCAL_NAV
straight out of AppShell.tsx and fails when the declaration in
apps/desktop-ui/surfaces.yaml and the real navigation drift apart — in either
direction — and when a forbidden route class is claimed as local.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml

MANIFEST = Path("apps/desktop-ui/surfaces.yaml")
APP_SHELL = Path("core/console/src/components/AppShell.tsx")
ROUTES_GLOB = "core/console/src/app/**/page.tsx"


def local_nav_routes(source: str) -> list[str]:
    """Routes listed in the LOCAL_NAV table of AppShell.tsx."""
    match = re.search(r"const LOCAL_NAV:\s*LocalNavItem\[\]\s*=\s*\[(.*?)\n\]", source, re.DOTALL)
    if not match:
        raise SystemExit(
            "LOCAL_NAV not found in AppShell.tsx — the gate cannot verify the "
            "perimeter against the source. If the table moved, update this parser."
        )
    return re.findall(r'href:\s*"([^"]+)"', match.group(1))


def exported_routes(root: Path) -> set[str]:
    """Every route the shared Next app exports, as URL paths."""
    routes = set()
    for page in root.glob(ROUTES_GLOB):
        rel = page.relative_to(root / "core/console/src/app").parent
        parts = [p for p in rel.parts if not (p.startswith("(") and p.endswith(")"))]
        routes.add("/" + "".join(f"{p}/" for p in parts))
    return routes


def validate(root: Path) -> list[str]:
    errors: list[str] = []
    manifest = yaml.safe_load((root / MANIFEST).read_text(encoding="utf-8"))

    declared = list(manifest.get("owned_routes") or [])
    if not declared:
        return ["surfaces.yaml declares no owned_routes — refusing to pass"]
    if manifest.get("owner_project") != "marvis":
        errors.append("surfaces.yaml: owner_project must be marvis")

    forbidden = manifest.get("forbidden_routes") or {}
    for route in declared:
        if route in forbidden:
            errors.append(f"{route}: claimed as local but listed as forbidden ({forbidden[route]})")

    in_code = local_nav_routes((root / APP_SHELL).read_text(encoding="utf-8"))
    missing = [route for route in in_code if route not in declared]
    extra = [route for route in declared if route not in in_code]
    for route in missing:
        errors.append(f"{route}: navigated by LOCAL_NAV but not declared in owned_routes")
    for route in extra:
        errors.append(f"{route}: declared as owned but not navigated by LOCAL_NAV")

    # Every declared route must actually exist as a page.
    exported = exported_routes(root)
    for route in declared:
        if route not in exported:
            errors.append(f"{route}: declared as owned but no page exports it")

    return errors


def foreign_routes(root: Path) -> list[str]:
    """Exported routes outside the local perimeter — the gap the move closes."""
    manifest = yaml.safe_load((root / MANIFEST).read_text(encoding="utf-8"))
    declared = set(manifest.get("owned_routes") or [])
    # The entry point and auth pages are shell, not product surfaces.
    shell = {"/", "/login/", "/login/sso-callback/", "/welcome/"}
    return sorted(exported_routes(root) - declared - shell)


def main() -> int:
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path.cwd()
    errors = validate(root)
    if errors:
        print("local surface perimeter INVALID:", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        return 1

    strangers = foreign_routes(root)
    print(
        "local surface perimeter valid: declaration matches LOCAL_NAV and every "
        "owned route exists"
    )
    if strangers:
        # Reported, not failed: this is the known pre-move state the manifest
        # records. It becomes a failure once the move is done.
        print(
            f"note: the shared export still carries {len(strangers)} routes outside "
            "the local perimeter (U7 move closes this)"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
