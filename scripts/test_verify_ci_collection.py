from __future__ import annotations

import json
from pathlib import Path
import shutil
import tempfile
import unittest

import yaml

from run_ci_contract import _DESELECTED_RE
from verify_ci_collection import (
    CollectionContractError,
    _platform_matrix,
    _workflow,
    verify,
)


ROOT = Path(__file__).resolve().parents[1]


class CICollectionContractTests(unittest.TestCase):
    def test_current_collection_and_workflows_pass(self) -> None:
        result = verify(ROOT)
        # 444 API + 32 CLI tests from a clean checkout. Keep this independent
        # from generated Console assets and other local build by-products.
        self.assertGreaterEqual(result["pytest_collected"], 476)
        self.assertGreaterEqual(result["unittest_collected"], 228)

    def test_lowered_collection_floor_is_not_a_bypass(self) -> None:
        with tempfile.TemporaryDirectory(prefix="marvis-ci-contract-") as raw:
            root = Path(raw)
            shutil.copytree(ROOT / "contracts", root / "contracts")
            shutil.copytree(ROOT / ".github", root / ".github")
            path = root / "contracts/ci/collection-v1.json"
            contract = json.loads(path.read_text())
            contract["pytest_suites"][0]["path"] = "missing-suite"
            path.write_text(json.dumps(contract))
            with self.assertRaisesRegex(CollectionContractError, "path missing"):
                verify(root)

    def test_deselection_summary_is_machine_detectable(self) -> None:
        output = "415 passed, 3 deselected in 1.23s\n"
        self.assertEqual(_DESELECTED_RE.findall(output), ["3"])

    def test_primary_python_row_cannot_drift(self) -> None:
        contract = json.loads(
            (ROOT / "contracts/ci/collection-v1.json").read_text(encoding="utf-8")
        )
        contract["primary_python"] = "3.11"
        with self.assertRaisesRegex(CollectionContractError, "Python row drift"):
            _workflow(ROOT, contract)

    def test_echoed_primary_release_state_is_not_a_gate(self) -> None:
        contract = json.loads(
            (ROOT / "contracts/ci/collection-v1.json").read_text(encoding="utf-8")
        )
        with tempfile.TemporaryDirectory(prefix="marvis-primary-ci-") as raw:
            root = Path(raw)
            shutil.copytree(ROOT / ".github", root / ".github")
            workflow = root / ".github/workflows/ci.yml"
            workflow.write_text(
                workflow.read_text(encoding="utf-8").replace(
                    "run: python scripts/release_candidate.py state",
                    "run: echo python scripts/release_candidate.py state",
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(CollectionContractError, "CI run line missing"):
                _workflow(root, contract)

    def test_release_ancestry_job_cannot_return_to_a_shallow_checkout(self) -> None:
        workflow = yaml.safe_load(
            (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
        )
        steps = workflow["jobs"]["python-contract"]["steps"]
        checkout = [
            step
            for step in steps
            if str(step.get("uses", "")).startswith("actions/checkout@")
        ]
        self.assertEqual(len(checkout), 1)
        self.assertEqual(checkout[0].get("with", {}).get("fetch-depth"), 0)

    def test_desktop_release_build_cannot_leave_ci(self) -> None:
        contract = json.loads(
            (ROOT / "contracts/ci/collection-v1.json").read_text(encoding="utf-8")
        )
        contract["workflow"]["additional_jobs"][0]["required_run_lines"].append(
            "missing desktop release gate"
        )
        with self.assertRaisesRegex(CollectionContractError, "CI run line missing"):
            _workflow(ROOT, contract)

    def test_desktop_build_sha_cannot_drift(self) -> None:
        contract = json.loads(
            (ROOT / "contracts/ci/collection-v1.json").read_text(encoding="utf-8")
        )
        contract["workflow"]["additional_jobs"][0]["required_step_env"][
            "MARVIS_CONSOLE_BUILD_ID"
        ] = "main"
        with self.assertRaisesRegex(CollectionContractError, "step environment drift"):
            _workflow(ROOT, contract)

    def test_echoed_desktop_build_is_not_a_gate(self) -> None:
        contract = json.loads(
            (ROOT / "contracts/ci/collection-v1.json").read_text(encoding="utf-8")
        )
        with tempfile.TemporaryDirectory(prefix="marvis-desktop-ci-") as raw:
            root = Path(raw)
            shutil.copytree(ROOT / ".github", root / ".github")
            workflow = root / ".github/workflows/ci.yml"
            workflow.write_text(
                workflow.read_text(encoding="utf-8").replace(
                    "          npm run build\n", "          echo npm run build\n"
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(CollectionContractError, "CI run line missing"):
                _workflow(root, contract)

    def test_supported_os_row_cannot_disappear(self) -> None:
        contract = json.loads(
            (ROOT / "contracts/ci/collection-v1.json").read_text(encoding="utf-8")
        )
        contract["platform_matrix"]["operating_systems"].append("plan9-latest")
        with self.assertRaisesRegex(CollectionContractError, "OS row missing"):
            _platform_matrix(ROOT, contract)

    def test_min_python_public_claim_gate_provisions_tomli(self) -> None:
        workflow = (ROOT / ".github/workflows/e2e-macos.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("uv run --no-project", workflow)
        self.assertIn("tomli>=2; python_version < '3.11'", workflow)


if __name__ == "__main__":
    unittest.main()
