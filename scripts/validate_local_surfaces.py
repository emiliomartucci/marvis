#!/usr/bin/env python3
"""Fail-closed gate for the local GUI ownership perimeter (U7).

The perimeter is only meaningful if it matches the code. This reads the real
source of apps/desktop-ui and fails when it drifts from the declaration in
apps/desktop-ui/surfaces.yaml:

  1. LOCAL_NAV in AppShell.tsx and owned_routes must agree, in both directions.
  2. Every declared route must exist as a page, and every exported page must be
     declared.
  3. Every internal link and router navigation inside the code that actually
     ships must target a declared route.
  4. Every module in the source tree must be reachable from a route.

Checks 3 and 4 exist because the first version of this gate verified pages and
the navigation table only. It passed while the shipped bundle still mounted the
terminal panel, still carried the hosted top bar, and still linked to
/terminal/, /settings/users/ and /brain/diario/ — routes this product does not
ship. Pages were never the perimeter: the emitted JavaScript is.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml

DESKTOP_UI = Path("apps/desktop-ui")
MANIFEST = DESKTOP_UI / "surfaces.yaml"
APP_SHELL = DESKTOP_UI / "src/components/AppShell.tsx"
SRC = DESKTOP_UI / "src"
APP_DIR = SRC / "app"
ROUTES_GLOB = "apps/desktop-ui/src/app/**/page.tsx"
# Entry point and auth pages: shell, not product surfaces.
SHELL_ROUTES = {"/", "/login/", "/login/sso-callback/", "/welcome/"}
# Files Next.js loads as route entry points.
ROUTE_FILES = {"page.tsx", "layout.tsx", "template.tsx", "error.tsx", "not-found.tsx", "loading.tsx"}
# Reachable from the toolchain rather than from a route.
TOOLCHAIN_MODULES = {SRC / "test/setup.ts"}

IMPORT_RE = re.compile(r"""(?:from\s+|import\s*\(\s*)["']([^"']+)["']""")
# Anything that puts the user on a route: a plain href, a braced one, a template
# literal, a router call. The first version required a quote immediately after
# `href=`, so `href={`/admin/?id=${id}`}` was invisible to it and only got
# reported if /admin/ happened to sit in forbidden_routes — a finite list, so
# every other undeclared route shipped silently.
LINK_RE = re.compile(
    r"""(?:href=\{?\s*|router\.(?:push|replace)\(\s*)["'`](/[^"'`?#$\s]*)""",
)
# Nav tables declare their targets as object fields, not JSX attributes.
NAV_FIELD_RE = re.compile(r"""\bhref:\s*["'`](/[^"'`?#$\s]*)""")
# A route reached by building the URL: `/finder/?path=` + something, or
# `${origin}/graph/?id=...`. The first version of this gate read only href
# attributes and missed both. The leading `}` matters: the share link that got
# through was a path glued to the end of a template interpolation.
PATH_LITERAL_RE = re.compile(
    r"""(?P<before>["'`}])(?P<path>/[a-z0-9][a-z0-9\-/]*/?)(?=[?#"'`$])""", re.IGNORECASE
)
# Prefixes that are not GUI routes: server endpoints and build-time assets.
NON_ROUTE_PREFIXES = ("/api/", "/_next/", "/ui/")
# `${API_BASE_URL}/terminal/upload` is a server call, not a navigation. Only the
# interpolated base tells the two apart.
API_BASE_RE = re.compile(r"\$\{[^}]*(?:API|api)[A-Za-z_]*\}$")


def is_test(path: Path) -> bool:
    return "__tests__" in path.parts or ".test." in path.name or ".spec." in path.name


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
    """Every route the local Next app exports, as URL paths."""
    routes = set()
    for page in root.glob(ROUTES_GLOB):
        rel = page.relative_to(root / DESKTOP_UI / "src/app").parent
        parts = [p for p in rel.parts if not (p.startswith("(") and p.endswith(")"))]
        routes.add("/" + "".join(f"{p}/" for p in parts))
    return routes


def _resolve_import(spec: str, importer: Path, root: Path) -> Path | None:
    """Resolve a TS import specifier to a file, or None if it is a package."""
    if spec.startswith("@/"):
        base = root / SRC / spec[2:]
    elif spec.startswith("."):
        base = (importer.parent / spec).resolve()
    else:
        return None
    if base.is_file() and base.suffix in {".ts", ".tsx"}:
        return base
    for candidate in (Path(f"{base}.ts"), Path(f"{base}.tsx"), base / "index.ts", base / "index.tsx"):
        if candidate.is_file():
            return candidate
    return None


def shipped_modules(root: Path) -> set[Path]:
    """Transitive import closure of the route entry points.

    This is what the bundler emits: a module nobody imports from a route does
    not reach the user, and a module imported from a route does — whatever a
    runtime flag decides to render.
    """
    entries = [p for p in (root / APP_DIR).rglob("*.tsx") if p.name in ROUTE_FILES and not is_test(p)]
    if not entries:
        raise SystemExit(f"no route entry points under {APP_DIR} — refusing to pass")
    seen: set[Path] = set()
    stack = list(entries)
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        for spec in IMPORT_RE.findall(current.read_text(encoding="utf-8")):
            target = _resolve_import(spec, current, root)
            if target is not None and target not in seen:
                stack.append(target)
    return seen


def strip_comments(source: str) -> str:
    """Drop comment lines so prose about a surface is not read as a link to it.

    Conservative on purpose: only whole lines that start a comment, never an
    inline `//` that could sit inside a string such as an https:// URL.
    """
    kept = []
    for line in source.split("\n"):
        stripped = line.lstrip()
        if stripped.startswith(("//", "/*", "*/", "*")):
            continue
        kept.append(line)
    return "\n".join(kept)


def navigation_targets(module: Path) -> set[str]:
    """Internal routes this module sends the user to, declared as links.

    Every hit is compared against the declared perimeter, not against a list of
    known-bad routes: a route nobody thought to forbid is still a route this
    product does not ship.
    """
    source = strip_comments(module.read_text(encoding="utf-8"))
    targets = set(LINK_RE.findall(source)) | set(NAV_FIELD_RE.findall(source))
    normalised = set()
    for target in targets:
        if target.startswith(NON_ROUTE_PREFIXES):
            continue
        # A trailing filename is an asset request, not a navigation.
        if "." in target.rsplit("/", 1)[-1]:
            continue
        normalised.add(target if target.endswith("/") else f"{target}/")
    return normalised


def forbidden_literals(module: Path, forbidden: set[str]) -> set[str]:
    """Forbidden routes appearing as a URL this module builds.

    A link does not have to be written as `href="/finder/"` to reach the user:
    the universe surface assembled `/finder/?path=` + a project path, which the
    href-only check above walked straight past.
    """
    hits = set()
    source = strip_comments(module.read_text(encoding="utf-8"))
    for match in PATH_LITERAL_RE.finditer(source):
        literal = match.group("path")
        if literal.startswith(NON_ROUTE_PREFIXES):
            continue
        if match.group("before") == "}" and API_BASE_RE.search(source[: match.start() + 1]):
            continue
        for route in forbidden:
            if literal == route or literal.startswith(route):
                hits.add(route)
    return hits


def validate(root: Path) -> list[str]:
    # Resolved once, here: the import walker turns relative imports into
    # absolute paths while `@/` ones stay as given, so a relative root mixed
    # the two forms and `relative_to` raised instead of reporting findings.
    root = root.resolve()
    errors: list[str] = []
    manifest = yaml.safe_load((root / MANIFEST).read_text(encoding="utf-8"))

    navigated = list(manifest.get("owned_routes") or [])
    if not navigated:
        return ["surfaces.yaml declares no owned_routes — refusing to pass"]
    # Routes reached from a flow rather than the nav bar. Each must state which
    # flow needs it, so this cannot become a backdoor for unnavigated surfaces.
    reachable = manifest.get("reachable_routes") or {}
    for route, reason in reachable.items():
        if not str(reason or "").strip():
            errors.append(f"{route}: declared reachable without naming the flow that needs it")
    declared = navigated + list(reachable)
    if manifest.get("owner_project") != "marvis":
        errors.append("surfaces.yaml: owner_project must be marvis")

    forbidden = manifest.get("forbidden_routes") or {}
    for route in declared:
        if route in forbidden:
            errors.append(f"{route}: claimed as local but listed as forbidden ({forbidden[route]})")

    in_code = local_nav_routes((root / APP_SHELL).read_text(encoding="utf-8"))
    for route in in_code:
        if route not in navigated:
            errors.append(f"{route}: navigated by LOCAL_NAV but not declared in owned_routes")
    for route in navigated:
        if route not in in_code:
            errors.append(f"{route}: declared as owned but not navigated by LOCAL_NAV")

    # Every declared route must actually exist as a page.
    exported = exported_routes(root)
    for route in declared:
        if route not in exported:
            errors.append(f"{route}: declared as owned but no page exports it")

    # The source is now the perimeter: a foreign route here is a defect, not a
    # known gap. Until slice 4 this was reported; it is enforced from here on.
    for route in sorted(exported - set(declared) - SHELL_ROUTES):
        errors.append(f"{route}: present in the local source but outside the perimeter")

    shipped = shipped_modules(root)
    known_routes = set(declared) | SHELL_ROUTES | exported
    for module in sorted(shipped):
        rel = module.relative_to(root)
        for target in sorted(navigation_targets(module)):
            if target not in known_routes:
                errors.append(f"{rel}: navigates to {target}, which this product does not ship")
        for target in sorted(forbidden_literals(module, set(forbidden))):
            errors.append(f"{rel}: builds a URL for {target} ({forbidden[target]})")

    # Source the product cannot reach is source it should not carry: it is what
    # let the terminal, hosted administration and SaaS components travel into
    # this repository unnoticed.
    all_modules = {
        p
        for p in (root / SRC).rglob("*.ts*")
        if p.suffix in {".ts", ".tsx"} and not is_test(p)
    }
    orphans = all_modules - shipped - {root / m for m in TOOLCHAIN_MODULES}
    for module in sorted(orphans):
        errors.append(f"{module.relative_to(root)}: no route reaches this module")

    return errors


def declared_routes(root: Path) -> set[str]:
    """Everything the local product owns: navigated, reachable, and shell."""
    root = root.resolve()
    manifest = yaml.safe_load((root / MANIFEST).read_text(encoding="utf-8"))
    return (
        set(manifest.get("owned_routes") or [])
        | set(manifest.get("reachable_routes") or {})
        | SHELL_ROUTES
    )


def foreign_routes(root: Path) -> list[str]:
    """Exported routes outside the local perimeter."""
    root = root.resolve()
    return sorted(exported_routes(root) - declared_routes(root))


def main() -> int:
    root = (Path(sys.argv[1]) if len(sys.argv) > 1 else Path.cwd()).resolve()
    errors = validate(root)
    if errors:
        print("local surface perimeter INVALID:", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        return 1

    print(
        "local surface perimeter valid: the shipped source carries the declared "
        "routes, navigates nowhere else, and holds no module the product cannot reach"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
