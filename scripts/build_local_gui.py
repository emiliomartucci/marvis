#!/usr/bin/env python3
"""Prepare the local GUI build tree from its declared perimeter (U7).

The local product ships a Next static export. Today that export is the whole
application, so routes owned by other products — the terminal, hosted
administration, SaaS surfaces — travel inside the local wheel and answer to a
direct URL even though navigation hides them.

This prunes a copy of the console source down to the perimeter declared in
apps/desktop-ui/surfaces.yaml before the build runs, so the artifact can only
contain what the local product owns. The source tree is never modified: the
pruning happens in a build directory.

Usage:
  python scripts/build_local_gui.py <output-dir>   # prepare the pruned tree
  python scripts/build_local_gui.py --check        # perimeter check only
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from validate_local_surfaces import validate  # noqa: E402

CONSOLE = Path("core/console")
APP_DIR = CONSOLE / "src" / "app"
MANIFEST = Path("apps/desktop-ui/surfaces.yaml")
# Shell routes: the entry point and auth pages are not product surfaces, and
# the local product needs them to boot.
SHELL_ROUTES = {"/", "/login/", "/login/sso-callback/", "/welcome/"}


def route_of(page: Path, app_dir: Path) -> str:
    """URL path of a page file, ignoring Next route groups."""
    rel = page.relative_to(app_dir).parent
    parts = [p for p in rel.parts if not (p.startswith("(") and p.endswith(")"))]
    return "/" + "".join(f"{p}/" for p in parts)


def owned_routes(root: Path) -> set[str]:
    manifest = yaml.safe_load((root / MANIFEST).read_text(encoding="utf-8"))
    return set(manifest.get("owned_routes") or []) | SHELL_ROUTES


def prune(tree: Path, keep: set[str]) -> list[str]:
    """Delete every page outside the perimeter. Returns the removed routes.

    A route directory can contain nested routes, and a kept route may live
    under a removed one. Deepest-first, and never delete a directory that
    still holds a page we keep — in that case only the route's own files go.
    """
    app_dir = tree / "src" / "app"
    pages = sorted(app_dir.rglob("page.tsx"), key=lambda p: len(p.parts), reverse=True)
    removed = []

    for page in pages:
        if not page.exists():  # its parent route was already removed
            continue
        route = route_of(page, app_dir)
        if route in keep:
            continue

        directory = page.parent
        holds_kept = any(
            route_of(nested, app_dir) in keep
            for nested in directory.rglob("page.tsx")
            if nested != page
        )
        if holds_kept:
            # Drop only this route's own files; the nested kept route stays.
            for item in directory.iterdir():
                if item.is_file():
                    item.unlink()
        else:
            shutil.rmtree(directory)
        removed.append(route)

    return removed


def main() -> int:
    root = Path.cwd()

    # A pruned tree built from a stale perimeter would ship the wrong product.
    errors = validate(root)
    if errors:
        print("refusing to build: the declared perimeter does not match the source", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        return 1

    if "--check" in sys.argv[1:]:
        print("perimeter consistent with the source")
        return 0

    if len(sys.argv) < 2:
        print("usage: build_local_gui.py <output-dir> | --check", file=sys.stderr)
        return 2

    out = Path(sys.argv[1]).resolve()
    if out.exists():
        shutil.rmtree(out)
    shutil.copytree(
        root / CONSOLE,
        out,
        ignore=shutil.ignore_patterns("node_modules", "out", ".next", "playwright"),
    )

    keep = owned_routes(root)
    removed = prune(out, keep)

    remaining = {
        route_of(page, out / "src" / "app") for page in (out / "src" / "app").rglob("page.tsx")
    }
    foreign = sorted(remaining - keep)
    if foreign:
        print(f"pruning failed, still present: {foreign}", file=sys.stderr)
        return 1

    print(f"pruned build tree at {out}")
    print(f"  kept {len(remaining)} routes, removed {len(removed)}")
    print(f"  removed: {', '.join(removed)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
