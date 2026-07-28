"""The gate must be proven RED on broken inputs, not only green on good ones
(marvisx learning d3b7f373: a gate never seen failing proves nothing)."""

from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from validate_surfaces import validate  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]


class FixtureCase(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="surfaces-"))
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        shutil.copytree(REPO_ROOT / "contracts", self.dir / "contracts")
        shutil.copy(REPO_ROOT / "pyproject.toml", self.dir / "pyproject.toml")

    def rewrite(self, rel: str, old: str, new: str) -> None:
        path = self.dir / rel
        path.write_text(path.read_text(encoding="utf-8").replace(old, new), encoding="utf-8")


class TestValidator(FixtureCase):
    def test_real_repo_state_is_green(self) -> None:
        self.assertEqual(validate(REPO_ROOT), [])

    def test_red_wrong_distribution_name(self) -> None:
        self.rewrite(
            "contracts/surfaces/marvis-cli.yaml",
            "distribution_name: marvisx-cli",
            "distribution_name: some-other-wheel",
        )
        errors = validate(self.dir)
        self.assertTrue(any("distribution_name" in e for e in errors), errors)

    def test_red_foreign_hostname_claim(self) -> None:
        self.rewrite(
            "contracts/surfaces/marvis-cli.yaml",
            "artifact_kind: python-wheel",
            "artifact_kind: python-wheel\nallowed_hostnames:\n  - console.justaskmarvis.com",
        )
        errors = validate(self.dir)
        self.assertTrue(any("owned by another product" in e for e in errors), errors)

    def test_red_desktop_ui_flipped_without_proof(self) -> None:
        self.rewrite(
            "contracts/surfaces/desktop-ui.yaml", "deployable: false", "deployable: true"
        )
        errors = validate(self.dir)
        self.assertTrue(any("prerequisites without proof" in e for e in errors), errors)

    def test_red_wrong_owner_project(self) -> None:
        self.rewrite(
            "contracts/surfaces/marvis-cli.yaml",
            "owner_project: marvis",
            "owner_project: marvisx",
        )
        errors = validate(self.dir)
        self.assertTrue(any("owner_project" in e for e in errors), errors)

    def test_red_tampered_engine_pin(self) -> None:
        self.rewrite(
            "contracts/engine-pin.yaml",
            "engine_ref: c02ee4adba4d5130be4ae6beeb43220c28986bde",
            "engine_ref: not-a-sha",
        )
        errors = validate(self.dir)
        self.assertTrue(any("engine_ref" in e for e in errors), errors)


if __name__ == "__main__":
    unittest.main()
