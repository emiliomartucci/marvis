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
DESKTOP = "contracts/surfaces/desktop-ui.yaml"
CLI = "contracts/surfaces/marvis-cli.yaml"
RELEASE = ".github/workflows/release.yml"
# Files the manifests point at. The validator resolves the evidence, so the
# fixture has to carry it or every case would go red for the wrong reason.
EVIDENCE = (
    "core/cli/tests/test_marvis_console_characterization.py",
    "contracts/desktop-host.yaml",
    "scripts/validate_local_surfaces.py",
    "docs/decisions/desktop-shell-selection.md",
)


class FixtureCase(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="surfaces-"))
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        shutil.copytree(REPO_ROOT / "contracts", self.dir / "contracts")
        shutil.copy(REPO_ROOT / "pyproject.toml", self.dir / "pyproject.toml")
        (self.dir / "apps/desktop-ui").mkdir(parents=True)
        for rel in (RELEASE, *EVIDENCE):
            target = self.dir / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(REPO_ROOT / rel, target)

    def rewrite(self, rel: str, old: str, new: str) -> None:
        path = self.dir / rel
        text = path.read_text(encoding="utf-8")
        assert old in text, f"fixture anchor not found in {rel}: {old!r}"
        path.write_text(text.replace(old, new), encoding="utf-8")


class TestValidator(FixtureCase):
    def test_real_repo_state_is_green(self) -> None:
        self.assertEqual(validate(REPO_ROOT), [])

    def test_fixture_is_green_before_tampering(self) -> None:
        # Otherwise a red case below could pass for a reason it never tested.
        self.assertEqual(validate(self.dir), [])

    def test_red_wrong_distribution_name(self) -> None:
        self.rewrite(CLI, "distribution_name: marvisx-cli", "distribution_name: some-other-wheel")
        errors = validate(self.dir)
        self.assertTrue(any("distribution_name" in e for e in errors), errors)

    def test_red_missing_distribution_name(self) -> None:
        # Deleting the field used to make the wheel cross-check silent: the
        # comparison was guarded by `declared and ...`, so absent read as fine.
        self.rewrite(CLI, "distribution_name: marvisx-cli\n", "")
        errors = validate(self.dir)
        self.assertTrue(
            any("distribution_name must be a non-empty string" in e for e in errors), errors
        )

    def test_red_foreign_hostname_claim(self) -> None:
        self.rewrite(
            CLI,
            "artifact_kind: python-wheel",
            "artifact_kind: python-wheel\nallowed_hostnames:\n  - console.justaskmarvis.com",
        )
        errors = validate(self.dir)
        self.assertTrue(any("owned by another product" in e for e in errors), errors)

    def test_red_foreign_hostname_in_a_different_spelling(self) -> None:
        # DNS reads these as the same host; a raw membership test read them as
        # strangers and let the claim through.
        for spelling in ("Console.JustAskMarvis.com", "console.justaskmarvis.com.", "CONSOLE.justaskmarvis.COM"):
            with self.subTest(spelling=spelling):
                self.setUp()
                self.rewrite(
                    CLI,
                    "artifact_kind: python-wheel",
                    f"artifact_kind: python-wheel\nallowed_hostnames:\n  - {spelling}",
                )
                errors = validate(self.dir)
                self.assertTrue(any("owned by another product" in e for e in errors), errors)

    def test_red_wrong_owner_project(self) -> None:
        self.rewrite(CLI, "owner_project: marvis", "owner_project: marvisx")
        errors = validate(self.dir)
        self.assertTrue(any("owner_project" in e for e in errors), errors)

    def test_red_falsy_owner_values(self) -> None:
        # `0` satisfied the old required-field check by explicit exemption and
        # then skipped both comparisons behind a truthiness guard.
        self.rewrite(CLI, "owner_project: marvis", "owner_project: 0")
        self.rewrite(CLI, "owner_repo: emiliomartucci/marvis", "owner_repo: 0")
        errors = validate(self.dir)
        self.assertTrue(any("owner_project" in e for e in errors), errors)
        self.assertTrue(any("owner_repo" in e for e in errors), errors)

    def test_red_empty_owner_values(self) -> None:
        self.rewrite(CLI, "owner_project: marvis", 'owner_project: ""')
        errors = validate(self.dir)
        self.assertTrue(any("owner_project must be a non-empty string" in e for e in errors), errors)

    def test_red_prerequisites_replaced_with_an_arbitrary_key(self) -> None:
        # The old gate computed `unproven` from the manifest's own mapping, so
        # swapping in one evidenced key of any name passed.
        self.rewrite(
            DESKTOP,
            "prerequisites:\n"
            "  local_gui_characterization: core/cli/tests/test_marvis_console_characterization.py\n"
            "  desktop_host_contract: contracts/desktop-host.yaml\n"
            "  perimeter_gate: scripts/validate_local_surfaces.py\n",
            "prerequisites:\n  looks_fine_to_me: pyproject.toml\n",
        )
        errors = validate(self.dir)
        self.assertTrue(any("missing required keys" in e for e in errors), errors)
        self.assertTrue(any("unknown keys" in e for e in errors), errors)

    def test_red_prerequisite_pointing_at_nothing(self) -> None:
        self.rewrite(DESKTOP, "desktop_host_contract: contracts/desktop-host.yaml", "desktop_host_contract: contracts/imaginary.yaml")
        errors = validate(self.dir)
        self.assertTrue(any("does not exist in the tree" in e for e in errors), errors)

    def test_red_shipping_claim_the_release_does_not_honour(self) -> None:
        # `deployable: true` is a claim about the release, so it is read against
        # the release: a GUI nobody builds must not pass as shipped.
        self.rewrite(RELEASE, "-w /repo/apps/desktop-ui", "-w /repo/apps/some-other-ui")
        self.rewrite(RELEASE, "apps/desktop-ui/out", "apps/some-other-ui/out")
        errors = validate(self.dir)
        self.assertTrue(any("never builds apps/desktop-ui" in e for e in errors), errors)

    def test_red_shipping_claim_the_wheel_does_not_honour(self) -> None:
        self.rewrite("pyproject.toml", 'console_dist/**/*', "some_other_dir/**/*")
        errors = validate(self.dir)
        self.assertTrue(any("does not ship console_dist" in e for e in errors), errors)

    def test_red_desktop_shell_claimed_while_the_adr_is_open(self) -> None:
        self.rewrite(DESKTOP, "desktop_shell:\n  deployable: false", "desktop_shell:\n  deployable: true")
        errors = validate(self.dir)
        self.assertTrue(any("is not accepted" in e for e in errors), errors)

    def test_desktop_shell_allowed_once_the_adr_is_accepted(self) -> None:
        self.rewrite(DESKTOP, "desktop_shell:\n  deployable: false", "desktop_shell:\n  deployable: true")
        self.rewrite("docs/decisions/desktop-shell-selection.md", "status: open", "status: accepted")
        self.assertEqual(validate(self.dir), [])

    def test_red_tampered_engine_pin(self) -> None:
        self.rewrite(
            "contracts/engine-pin.yaml",
            "engine_ref: c02ee4adba4d5130be4ae6beeb43220c28986bde",
            "engine_ref: not-a-sha",
        )
        errors = validate(self.dir)
        self.assertTrue(any("engine_ref" in e for e in errors), errors)


class TestReleasePathRunsTheGate(unittest.TestCase):
    """A `v*` tag starts release.yml directly and skips the CI workflow.

    The registry gate has to run on the path that ships, not only on the path
    that reviews.
    """

    def test_release_workflow_runs_every_registry_gate(self) -> None:
        workflow = (REPO_ROOT / RELEASE).read_text(encoding="utf-8")
        for gate in (
            "scripts/validate_surfaces.py",
            "scripts/validate_local_surfaces.py",
            "scripts/validate_desktop_host.py",
        ):
            self.assertIn(gate, workflow, f"{gate} does not run on the tag path")

    def test_release_workflow_gates_before_it_builds(self) -> None:
        workflow = (REPO_ROOT / RELEASE).read_text(encoding="utf-8")
        self.assertLess(
            workflow.index("scripts/validate_surfaces.py"),
            workflow.index("Build the local GUI static export"),
            "the registry gate must run before anything is built",
        )


if __name__ == "__main__":
    unittest.main()
