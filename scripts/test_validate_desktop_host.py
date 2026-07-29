"""The desktop host contract must be proven RED when it drifts from the
launcher or when it quietly decides what it is not allowed to decide."""

from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from validate_desktop_host import (  # noqa: E402
    launcher_constants,
    registered_commands,
    validate,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
CONTRACT = "contracts/desktop-host.yaml"
LAUNCHER = "core/cli/marvis_console.py"


class ContractCase(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="desktop-host-"))
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        for rel in ("contracts", "docs/decisions", "apps/desktop-ui"):
            (self.dir / rel).mkdir(parents=True, exist_ok=True)
        shutil.copy(REPO_ROOT / CONTRACT, self.dir / CONTRACT)
        (self.dir / "core/cli").mkdir(parents=True)
        shutil.copy(REPO_ROOT / LAUNCHER, self.dir / LAUNCHER)
        shutil.copy(
            REPO_ROOT / "docs/decisions/desktop-shell-selection.md",
            self.dir / "docs/decisions/desktop-shell-selection.md",
        )
        shutil.copy(
            REPO_ROOT / "apps/desktop-ui/surfaces.yaml",
            self.dir / "apps/desktop-ui/surfaces.yaml",
        )

    def rewrite(self, rel: str, old: str, new: str) -> None:
        path = self.dir / rel
        path.write_text(path.read_text(encoding="utf-8").replace(old, new, 1), encoding="utf-8")


class TestParsing(unittest.TestCase):
    def test_endpoint_constants_come_from_the_launcher(self) -> None:
        constants = launcher_constants((REPO_ROOT / LAUNCHER).read_text(encoding="utf-8"))
        self.assertEqual(constants["_HOST"], "127.0.0.1")
        self.assertEqual(constants["_UI_PATH"], "/ui/")

    def test_registered_commands_include_subapp_verbs(self) -> None:
        commands = registered_commands((REPO_ROOT / LAUNCHER).read_text(encoding="utf-8"))
        self.assertIn("console", commands)
        self.assertIn("autostart enable", commands)


class TestContractGate(ContractCase):
    def test_real_contract_matches_the_real_launcher(self) -> None:
        self.assertEqual(validate(REPO_ROOT), [])

    def test_red_endpoint_drifts_from_the_launcher(self) -> None:
        # The launcher still opens 8100; a contract claiming otherwise would
        # send a shell to a port nothing serves.
        self.rewrite(CONTRACT, "port: 8100", "port: 9999")
        errors = validate(self.dir)
        self.assertTrue(any("endpoint port" in e for e in errors), errors)

    def test_red_capability_without_a_command(self) -> None:
        self.rewrite(
            CONTRACT,
            "  open_gui:\n    command: console",
            "  open_gui:\n    command: teleport",
        )
        errors = validate(self.dir)
        self.assertTrue(any("unknown command" in e for e in errors), errors)

    def test_red_runtime_leaves_loopback(self) -> None:
        self.rewrite(CONTRACT, "host: 127.0.0.1", "host: 0.0.0.0")
        errors = validate(self.dir)
        self.assertTrue(any("loopback" in e for e in errors), errors)

    def test_red_permissions_moved_to_the_shell(self) -> None:
        # A shell that decides for itself creates a second policy that drifts
        # from the runtime's.
        self.rewrite(CONTRACT, "owner: local-runtime", "owner: desktop-shell")
        errors = validate(self.dir)
        self.assertTrue(any("permissions owner" in e for e in errors), errors)

    def test_red_shell_technology_decided_here(self) -> None:
        # KTD4: this contract prepares for a shell, it does not pick one.
        self.rewrite(CONTRACT, "shell_selection: open", "shell_selection: decided")
        errors = validate(self.dir)
        self.assertTrue(any("must stay open" in e for e in errors), errors)

    def test_red_forbidden_rule_without_a_reason(self) -> None:
        self.rewrite(
            CONTRACT,
            "  direct_database_access: The runtime owns the database; a shell reads it through the API only.",
            "  direct_database_access:",
        )
        errors = validate(self.dir)
        self.assertTrue(any("no rationale" in e for e in errors), errors)

    def test_red_missing_decision_record(self) -> None:
        (self.dir / "docs/decisions/desktop-shell-selection.md").unlink()
        errors = validate(self.dir)
        self.assertTrue(any("shell_selection_record" in e for e in errors), errors)


if __name__ == "__main__":
    unittest.main()
