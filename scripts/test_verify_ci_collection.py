from __future__ import annotations

import json
from pathlib import Path
import shutil
import tempfile
import unittest

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
        # 416 API + 29 CLI tests from a clean checkout.  Keep this independent
        # from generated Console assets and other local build by-products.
        self.assertGreaterEqual(result["pytest_collected"], 445)
        self.assertGreaterEqual(result["unittest_collected"], 128)

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
