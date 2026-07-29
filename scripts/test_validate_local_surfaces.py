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
        for rel in ("apps",):
            shutil.copytree(REPO_ROOT / rel, self.dir / rel)

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
