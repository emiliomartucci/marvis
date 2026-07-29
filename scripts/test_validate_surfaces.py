"""The gate must be proven RED on broken inputs, not only green on good ones
(marvisx learning d3b7f373: a gate never seen failing proves nothing)."""

from __future__ import annotations

import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from validate_surfaces import release_run_commands, validate  # noqa: E402

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
        self.assertTrue(any("must name contracts/desktop-host.yaml" in e for e in errors), errors)

    def test_red_evidence_deleted_from_the_tree(self) -> None:
        # The manifest still names the right artifact; the artifact is gone.
        (self.dir / "contracts/desktop-host.yaml").unlink()
        errors = validate(self.dir)
        self.assertTrue(any("is not a file in the tree" in e for e in errors), errors)

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

    def test_desktop_shell_allowed_once_the_adr_is_accepted_and_something_packages_it(self) -> None:
        self.rewrite(DESKTOP, "desktop_shell:\n  deployable: false", "desktop_shell:\n  deployable: true")
        self.rewrite(
            DESKTOP,
            "  decision_record: docs/decisions/desktop-shell-selection.md",
            "  decision_record: docs/decisions/desktop-shell-selection.md\n"
            "  packaged_as:\n"
            "    built_by: apps/desktop-ui\n"
            "    artifact: a desktop application bundle",
        )
        self.rewrite("docs/decisions/desktop-shell-selection.md", "status: open", "status: accepted")
        self.assertEqual(validate(self.dir), [])

    def test_red_prerequisite_pointing_at_an_unrelated_file(self) -> None:
        # Checking only that the path exists let any file stand in for the proof
        # it was supposed to name.
        self.rewrite(DESKTOP, "desktop_host_contract: contracts/desktop-host.yaml", "desktop_host_contract: pyproject.toml")
        errors = validate(self.dir)
        self.assertTrue(any("must name contracts/desktop-host.yaml" in e for e in errors), errors)

    def test_red_prerequisite_pointing_at_a_directory(self) -> None:
        self.rewrite(DESKTOP, "perimeter_gate: scripts/validate_local_surfaces.py", "perimeter_gate: .")
        errors = validate(self.dir)
        self.assertTrue(any("must name scripts/validate_local_surfaces.py" in e for e in errors), errors)

    def test_red_served_at_drifting_from_the_host_contract(self) -> None:
        # The route is fixed by the desktop host contract; the shipping claim
        # could name any path and stay green.
        self.rewrite(DESKTOP, "served_at: /ui/", "served_at: /wrong/")
        errors = validate(self.dir)
        self.assertTrue(any("served_at" in e and "ui_path" in e for e in errors), errors)

    def test_red_non_boolean_desktop_shell_flag(self) -> None:
        # `"true"` is not `is True`, so the identity check read a malformed
        # truthy value as undeployable and skipped the ADR gate.
        self.rewrite(DESKTOP, "desktop_shell:\n  deployable: false", 'desktop_shell:\n  deployable: "true"')
        errors = validate(self.dir)
        self.assertTrue(any("must be a boolean" in e for e in errors), errors)

    def test_red_adr_accepted_only_inside_the_body(self) -> None:
        # A whole-document search matched an example in the prose and read an
        # open ADR as decided.
        self.rewrite(DESKTOP, "desktop_shell:\n  deployable: false", "desktop_shell:\n  deployable: true")
        self.rewrite(
            "docs/decisions/desktop-shell-selection.md",
            "## Status",
            "## Status\n\nA later ADR will read:\n\n```yaml\nstatus: accepted\n```\n",
        )
        errors = validate(self.dir)
        self.assertTrue(any("is not accepted" in e for e in errors), errors)

    def test_red_package_data_entry_commented_out(self) -> None:
        # Commenting the entry is how a setuptools setting is usually disabled;
        # the raw-text check still found the string and stayed green.
        self.rewrite(
            "pyproject.toml",
            '"core.api" = ["console_dist/**/*"]',
            '# "core.api" = ["console_dist/**/*"]',
        )
        errors = validate(self.dir)
        self.assertTrue(any("does not ship console_dist" in e for e in errors), errors)

    def test_red_broadened_shipping_paths(self) -> None:
        # `built_from: apps` and `bundled_at: console_dist` both matched the
        # workflow and the package data while naming neither the source nor the
        # bundle.
        self.rewrite(DESKTOP, "built_from: apps/desktop-ui", "built_from: apps")
        self.rewrite(DESKTOP, "bundled_at: core/api/console_dist", "bundled_at: console_dist")
        errors = validate(self.dir)
        self.assertTrue(any("built_from must be apps/desktop-ui" in e for e in errors), errors)
        self.assertTrue(any("bundled_at must be core/api/console_dist" in e for e in errors), errors)

    def test_red_package_data_under_the_wrong_package(self) -> None:
        self.rewrite("pyproject.toml", '"core.api" = ["console_dist/**/*"]', '"projects" = ["console_dist/**/*"]')
        errors = validate(self.dir)
        self.assertTrue(any("does not ship console_dist" in e for e in errors), errors)

    def test_red_decision_record_pointing_at_another_adr(self) -> None:
        # The manifest would otherwise pick whichever accepted ADR exists and
        # authorise itself with it.
        other = self.dir / "docs/decisions/something-else.md"
        other.write_text("---\nstatus: accepted\n---\n\n# Other\n", encoding="utf-8")
        self.rewrite(DESKTOP, "decision_record: docs/decisions/desktop-shell-selection.md", "decision_record: docs/decisions/something-else.md")
        self.rewrite(DESKTOP, "desktop_shell:\n  deployable: false", "desktop_shell:\n  deployable: true")
        errors = validate(self.dir)
        self.assertTrue(any("decision_record must be" in e for e in errors), errors)

    def test_red_shell_deployable_without_any_packaging(self) -> None:
        # An accepted ADR decides WHICH shell, not that one exists. Nothing
        # packages a desktop application today.
        self.rewrite(DESKTOP, "desktop_shell:\n  deployable: false", "desktop_shell:\n  deployable: true")
        self.rewrite("docs/decisions/desktop-shell-selection.md", "status: open", "status: accepted")
        errors = validate(self.dir)
        self.assertTrue(any("requires a packaged_as block" in e for e in errors), errors)

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
    that reviews. Read from the parsed workflow: a commented-out
    `# python scripts/validate_surfaces.py` still contains the script name, so a
    text search reported a gate that no longer executes.
    """

    def commands(self, root: Path) -> list[str]:
        return release_run_commands(root)

    @staticmethod
    def invokes(commands: list[str], script: str) -> bool:
        """The script must be what the line runs, not something it mentions."""
        return any(
            re.match(rf"^(python3?|py)\s+{re.escape(script)}(\s|$)", line) for line in commands
        )

    def test_release_workflow_runs_every_registry_gate(self) -> None:
        commands = self.commands(REPO_ROOT)
        for gate in (
            "scripts/validate_surfaces.py",
            "scripts/validate_local_surfaces.py",
            "scripts/validate_desktop_host.py",
        ):
            self.assertTrue(self.invokes(commands, gate), f"{gate} does not run on the tag path")

    def test_a_mentioned_gate_is_not_an_invoked_gate(self) -> None:
        workspace = Path(tempfile.mkdtemp(prefix="release-"))
        self.addCleanup(shutil.rmtree, workspace, ignore_errors=True)
        target = workspace / RELEASE
        target.parent.mkdir(parents=True)
        text = (REPO_ROOT / RELEASE).read_text(encoding="utf-8")
        target.write_text(
            text.replace(
                "          python scripts/validate_surfaces.py",
                "          echo python scripts/validate_surfaces.py",
            ),
            encoding="utf-8",
        )
        commands = self.commands(workspace)
        self.assertFalse(
            self.invokes(commands, "scripts/validate_surfaces.py"),
            "a script merely echoed is not a gate",
        )

    def test_release_workflow_gates_before_it_builds(self) -> None:
        commands = self.commands(REPO_ROOT)
        gate = next(i for i, l in enumerate(commands) if "scripts/validate_surfaces.py" in l)
        build = next(i for i, l in enumerate(commands) if "npm run build" in l)
        self.assertLess(gate, build, "the registry gate must run before anything is built")

    def test_a_commented_out_gate_is_not_a_gate(self) -> None:
        workspace = Path(tempfile.mkdtemp(prefix="release-"))
        self.addCleanup(shutil.rmtree, workspace, ignore_errors=True)
        target = workspace / RELEASE
        target.parent.mkdir(parents=True)
        text = (REPO_ROOT / RELEASE).read_text(encoding="utf-8")
        target.write_text(
            text.replace(
                "          python scripts/validate_surfaces.py",
                "          # python scripts/validate_surfaces.py",
            ),
            encoding="utf-8",
        )
        commands = self.commands(workspace)
        self.assertFalse(
            any("scripts/validate_surfaces.py" in line for line in commands),
            "a commented-out command must not count as a gate",
        )


if __name__ == "__main__":
    unittest.main()
