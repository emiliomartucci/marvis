"""The perimeter gate must be proven RED on drift, not only green today
(marvisx learning d3b7f373)."""

from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from validate_local_surfaces import (  # noqa: E402
    foreign_routes,
    local_nav_routes,
    validate,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
MANIFEST = "apps/desktop-ui/surfaces.yaml"
APP_SHELL = "apps/desktop-ui/src/components/AppShell.tsx"


class PerimeterCase(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="perimeter-"))
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        # Skip build output: a local `npm ci` leaves ~1 GB under apps/, and
        # copying it turns a sub-second suite into minutes.
        ignore = shutil.ignore_patterns("node_modules", ".next", "out")
        for rel in ("apps",):
            shutil.copytree(REPO_ROOT / rel, self.dir / rel, ignore=ignore)

    def rewrite(self, rel: str, old: str, new: str) -> None:
        path = self.dir / rel
        path.write_text(path.read_text(encoding="utf-8").replace(old, new, 1), encoding="utf-8")


class TestPerimeterGate(PerimeterCase):
    def test_real_repo_is_consistent(self) -> None:
        self.assertEqual(validate(REPO_ROOT), [])

    def test_local_nav_is_parsed_from_the_real_source(self) -> None:
        routes = local_nav_routes((REPO_ROOT / APP_SHELL).read_text(encoding="utf-8"))
        # Whatever the product navigates locally, it is these five today.
        self.assertEqual(len(routes), 5)
        self.assertIn("/tasks/", routes)

    def test_red_forbidden_route_claimed_as_local(self) -> None:
        # The terminal belongs to marvisx (plan R1/R4); claiming it here is the
        # exact confusion that caused the 2026-07-23 incident.
        self.rewrite(MANIFEST, "owned_routes:\n", "owned_routes:\n  - /terminal/\n")
        errors = validate(self.dir)
        self.assertTrue(any("/terminal/" in e and "forbidden" in e for e in errors), errors)

    def test_red_route_added_to_navigation_but_not_declared(self) -> None:
        self.rewrite(
            APP_SHELL,
            '{ key: "diario", href: "/diario/" },',
            '{ key: "diario", href: "/diario/" },\n  { key: "finder", href: "/finder/" },',
        )
        errors = validate(self.dir)
        self.assertTrue(
            any("/finder/" in e and "not declared" in e for e in errors), errors
        )

    def test_red_route_declared_but_not_navigated(self) -> None:
        self.rewrite(MANIFEST, "owned_routes:\n", "owned_routes:\n  - /finder/\n")
        errors = validate(self.dir)
        self.assertTrue(
            any("/finder/" in e and "not navigated" in e for e in errors), errors
        )

    def test_red_declared_route_without_a_page(self) -> None:
        self.rewrite(
            APP_SHELL,
            '{ key: "diario", href: "/diario/" },',
            '{ key: "diario", href: "/ghost/" },',
        )
        self.rewrite(MANIFEST, "  - /diario/\n", "  - /ghost/\n")
        errors = validate(self.dir)
        self.assertTrue(any("/ghost/" in e and "no page exports it" in e for e in errors), errors)

    def test_red_empty_perimeter(self) -> None:
        self.rewrite(MANIFEST, "owned_routes:", "owned_routes: []\n_unused:")
        errors = validate(self.dir)
        self.assertTrue(any("no owned_routes" in e for e in errors), errors)

    def test_red_shipped_code_links_to_a_route_the_product_lacks(self) -> None:
        # The defect this check exists for: the shell linked to /terminal/ and
        # /settings/users/ while every page-level check stayed green.
        self.rewrite(
            APP_SHELL,
            'href="/settings/llm/"',
            'href="/settings/users/"',
        )
        errors = validate(self.dir)
        self.assertTrue(
            any("/settings/users/" in e and "does not ship" in e for e in errors), errors
        )

    def test_red_shipped_code_navigates_to_a_route_the_product_lacks(self) -> None:
        self.rewrite(
            "apps/desktop-ui/src/app/(app)/page.tsx",
            'router.replace("/diario/");',
            'router.replace("/triage/");',
        )
        errors = validate(self.dir)
        self.assertTrue(any("/triage/" in e and "does not ship" in e for e in errors), errors)

    def test_red_nav_table_pointing_outside_the_perimeter(self) -> None:
        # Nav tables declare targets as object fields; the first version of this
        # gate only read JSX href attributes and missed a whole hosted top bar.
        self.rewrite(
            APP_SHELL,
            'const LOCAL_NAV: LocalNavItem[] = [',
            'const PACKAGES = [{ href: "/monitoring/" }];\n'
            'const LOCAL_NAV: LocalNavItem[] = [',
        )
        errors = validate(self.dir)
        self.assertTrue(any("/monitoring/" in e and "does not ship" in e for e in errors), errors)

    def test_red_braced_template_link_to_an_undeclared_route(self) -> None:
        # The check used to filter every assembled URL through forbidden_routes,
        # a finite list. A route nobody thought to forbid — /admin/ here — is
        # still a route this product does not ship.
        self.rewrite(
            "apps/desktop-ui/src/components/tasks/TaskSurface.tsx",
            "const VIEW_STORAGE_KEY",
            "const ADMIN = <a href={`/admin/?id=${1}`}>x</a>;\nconst VIEW_STORAGE_KEY",
        )
        errors = validate(self.dir)
        self.assertTrue(any("/admin/" in e and "does not ship" in e for e in errors), errors)

    def test_red_router_push_with_a_template_literal(self) -> None:
        # How the Codex lens navigated to /graph: a backtick, not a quote, so
        # the quote-anchored pattern walked past it.
        self.rewrite(
            "apps/desktop-ui/src/components/tasks/TaskSurface.tsx",
            "const VIEW_STORAGE_KEY",
            "const go = () => router.push(`/reports?x=${1}`);\nconst VIEW_STORAGE_KEY",
        )
        errors = validate(self.dir)
        self.assertTrue(any("/reports/" in e and "does not ship" in e for e in errors), errors)

    def test_a_relative_root_reports_findings_instead_of_raising(self) -> None:
        # `validate_local_surfaces.py .` used to raise ValueError: relative
        # imports resolved to absolute paths while `@/` ones stayed relative,
        # and relative_to could not reconcile the two. A crash is not a verdict.
        import os

        previous = Path.cwd()
        os.chdir(self.dir)
        try:
            self.assertEqual(validate(Path(".")), [])
        finally:
            os.chdir(previous)

    def test_red_url_assembled_from_a_forbidden_route(self) -> None:
        # The href-only check passed this: the universe surface built
        # `/finder/?path=` + a project path, and the finder is hosted-only.
        self.rewrite(
            "apps/desktop-ui/src/components/tasks/TaskSurface.tsx",
            "const VIEW_STORAGE_KEY",
            'const OPEN_IN_FINDER = `/finder/?path=${"x"}`;\nconst VIEW_STORAGE_KEY',
        )
        errors = validate(self.dir)
        self.assertTrue(any("/finder/" in e and "builds a URL" in e for e in errors), errors)

    def test_red_share_url_built_from_an_interpolated_origin(self) -> None:
        # How the graph share button got through: the path was glued to the end
        # of `${origin}`, so it never started right after a quote.
        self.rewrite(
            "apps/desktop-ui/src/components/tasks/TaskSurface.tsx",
            "const VIEW_STORAGE_KEY",
            "const SHARE = `${window.location.origin}/graph/?id=x`;\nconst VIEW_STORAGE_KEY",
        )
        errors = validate(self.dir)
        self.assertTrue(any("/graph/" in e and "builds a URL" in e for e in errors), errors)

    def test_server_call_on_an_api_base_is_not_a_navigation(self) -> None:
        # `${API_BASE_URL}/terminal/upload` is a request, not a link: only the
        # interpolated base separates it from the share URL above.
        self.rewrite(
            "apps/desktop-ui/src/components/tasks/TaskSurface.tsx",
            "const VIEW_STORAGE_KEY",
            "const UPLOAD = `${API_BASE_URL}/terminal/upload`;\nconst VIEW_STORAGE_KEY",
        )
        self.assertEqual(validate(self.dir), [])

    def test_prose_about_a_forbidden_route_is_not_a_link(self) -> None:
        # The check must read code, not comments: three of its first four hits
        # were sentences describing `/graph/cosmo`.
        self.rewrite(
            "apps/desktop-ui/src/components/tasks/TaskSurface.tsx",
            "const VIEW_STORAGE_KEY",
            '// The hosted build reaches `/graph/pr-impact` from here.\nconst VIEW_STORAGE_KEY',
        )
        self.assertEqual(validate(self.dir), [])

    def test_red_module_no_route_can_reach(self) -> None:
        # How the terminal, hosted administration and SaaS components travelled
        # into this repository: nothing imported them, nothing flagged them.
        orphan = self.dir / "apps/desktop-ui/src/components/OrphanPanel.tsx"
        orphan.write_text("export default function OrphanPanel() { return null; }\n", encoding="utf-8")
        errors = validate(self.dir)
        self.assertTrue(any("OrphanPanel.tsx" in e and "no route reaches" in e for e in errors), errors)


class TestPerimeterIsClosed(unittest.TestCase):
    """The same measurement that justified the move, now proving it landed.

    Before the move these two asserted the opposite: 24 foreign routes lived in
    the shared app, /terminal/ among them. They are the before/after evidence.
    """

    def test_owned_source_carries_no_foreign_route(self) -> None:
        self.assertEqual(foreign_routes(REPO_ROOT), [])

    def test_forbidden_classes_are_absent_from_the_owned_source(self) -> None:
        strangers = set(foreign_routes(REPO_ROOT))
        for route in ("/terminal/", "/settings/users/", "/monitoring/"):
            self.assertNotIn(route, strangers, f"{route} must not live in the local product")


if __name__ == "__main__":
    unittest.main()
