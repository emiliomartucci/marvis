"""The pruning must be proven to remove the right things and keep the rest.

The expensive proof — that the pruned tree actually compiles — was run against
the real source (Next build green, exporting exactly the perimeter). These
tests cover the pruning logic itself so a regression is caught without a build.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_local_gui import SHELL_ROUTES, owned_routes, prune, route_of  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]


def make_page(app_dir: Path, route_parts: list[str]) -> Path:
    directory = app_dir.joinpath(*route_parts)
    directory.mkdir(parents=True, exist_ok=True)
    page = directory / "page.tsx"
    page.write_text("export default function Page() { return null }\n", encoding="utf-8")
    return page


class TestRouteMapping(unittest.TestCase):
    def test_route_groups_are_not_part_of_the_url(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = Path(tmp)
            page = make_page(app, ["(app)", "tasks"])
            self.assertEqual(route_of(page, app), "/tasks/")

    def test_nested_route(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = Path(tmp)
            page = make_page(app, ["(app)", "settings", "users"])
            self.assertEqual(route_of(page, app), "/settings/users/")


class TestPruning(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="prune-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.app = self.tmp / "src" / "app"
        self.app.mkdir(parents=True)

    def test_keeps_owned_and_removes_foreign(self):
        make_page(self.app, ["(app)", "tasks"])
        make_page(self.app, ["(app)", "terminal"])
        make_page(self.app, ["(app)", "settings", "users"])

        removed = prune(self.tmp, {"/tasks/"})

        self.assertEqual(sorted(removed), ["/settings/users/", "/terminal/"])
        self.assertTrue((self.app / "(app)" / "tasks" / "page.tsx").exists())
        self.assertFalse((self.app / "(app)" / "terminal").exists())

    def test_a_kept_route_nested_under_a_removed_one_survives(self):
        # The failure this guards against: removing /brain/ would take
        # /brain/diario/ with it if the parent directory were deleted whole.
        make_page(self.app, ["(app)", "brain"])
        make_page(self.app, ["(app)", "brain", "diario"])

        removed = prune(self.tmp, {"/brain/diario/"})

        self.assertEqual(removed, ["/brain/"])
        self.assertTrue((self.app / "(app)" / "brain" / "diario" / "page.tsx").exists())
        self.assertFalse((self.app / "(app)" / "brain" / "page.tsx").exists())

    def test_pruning_is_idempotent(self):
        make_page(self.app, ["(app)", "tasks"])
        make_page(self.app, ["(app)", "terminal"])

        first = prune(self.tmp, {"/tasks/"})
        second = prune(self.tmp, {"/tasks/"})

        self.assertEqual(first, ["/terminal/"])
        self.assertEqual(second, [])

    def test_nothing_survives_outside_the_perimeter(self):
        for parts in (["(app)", "tasks"], ["(app)", "terminal"], ["(app)", "monitoring"]):
            make_page(self.app, parts)

        prune(self.tmp, {"/tasks/"})

        remaining = {route_of(p, self.app) for p in self.app.rglob("page.tsx")}
        self.assertEqual(remaining, {"/tasks/"})


class TestPerimeterSource(unittest.TestCase):
    def test_shell_routes_are_kept_alongside_the_declared_ones(self):
        keep = owned_routes(REPO_ROOT)
        # Product surfaces come from the manifest; the shell must survive too
        # or the local product cannot boot.
        self.assertIn("/tasks/", keep)
        self.assertTrue(SHELL_ROUTES.issubset(keep))

    def test_forbidden_routes_are_not_in_the_kept_set(self):
        keep = owned_routes(REPO_ROOT)
        for route in ("/terminal/", "/settings/users/", "/monitoring/"):
            self.assertNotIn(route, keep)


if __name__ == "__main__":
    unittest.main()
