from __future__ import annotations

import json
from pathlib import Path
import shutil
import tempfile
import unittest

from verify_compatibility_contract import CompatibilityError, verify


ROOT = Path(__file__).resolve().parents[1]
SOURCE_REF = "1245a57f18aa74a69ed7db6b42fc4516b7ae1e8b"
PAYLOAD_SHA256 = "dd9083bd517b52d08175c31d26647aba552b1408be0196007521d669effab465"


class CompatibilityContractTests(unittest.TestCase):
    def _copy(self) -> Path:
        temp = Path(tempfile.mkdtemp(prefix="marvis-compat-test-"))
        self.addCleanup(shutil.rmtree, temp, True)
        for relative in (
            "contracts/compatibility",
            "contracts/openapi",
            "contracts/engine-pin.yaml",
            "core/api/db.py",
            "core/api/mcp/stdio.py",
            "core/api/routers/agent_tokens.py",
            "core/api/routers/auth.py",
            "core/api/routers/graph_ingest.py",
            "core/api/services/schema_upgrade.py",
            "core/api/tests/test_require_scope_empty_deny.py",
            "core/api/tests/test_schema_compatibility.py",
            "core/cli/marvis_hooks.py",
            "core/cli/tests/test_marvis_console_characterization.py",
            "core/scripts/graph_export_client.py",
            "apps/desktop-ui/src",
            ".github/workflows/e2e-macos.yml",
            "README.md",
            "scripts/validate_local_surfaces.py",
            "scripts/verify_hook_policy.py",
            "scripts/verify_local_upgrade.py",
        ):
            source = ROOT / relative
            target = temp / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            if source.is_dir():
                shutil.copytree(source, target)
            else:
                shutil.copy2(source, target)
        return temp

    def test_current_contract_passes(self) -> None:
        result = verify(ROOT, expected_source_ref=SOURCE_REF)
        self.assertEqual(result["source_ref"], SOURCE_REF)
        self.assertEqual(result["projection_payload_sha256"], PAYLOAD_SHA256)
        self.assertGreater(result["n_operations"], result["n_minus_1_operations"])

    def test_manifest_tamper_fails_closed(self) -> None:
        root = self._copy()
        path = root / "contracts/compatibility/fixtures/n-contract.json"
        path.write_bytes(path.read_bytes() + b" ")
        with self.assertRaisesRegex(CompatibilityError, "digest mismatch"):
            verify(root, expected_source_ref=SOURCE_REF)

    def test_deliberate_break_that_no_longer_breaks_is_rejected(self) -> None:
        root = self._copy()
        fixtures = root / "contracts/compatibility/fixtures"
        baseline = json.loads((fixtures / "n-minus-1-contract.json").read_text())
        broken_path = fixtures / "deliberate-break.json"
        broken = json.loads(broken_path.read_text())
        declaration = broken["deliberate_break"]
        baseline["deliberate_break"] = declaration
        broken_path.write_text(json.dumps(baseline, sort_keys=True, indent=2) + "\n")
        manifest_path = fixtures / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        import hashlib

        manifest["files"]["deliberate-break.json"] = hashlib.sha256(
            broken_path.read_bytes()
        ).hexdigest()
        manifest_path.write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n")
        with self.assertRaisesRegex(CompatibilityError, "did not fail"):
            verify(root, expected_source_ref=SOURCE_REF)

    def test_missing_surface_row_is_rejected(self) -> None:
        root = self._copy()
        path = root / "contracts/compatibility/consumer-matrix-v1.json"
        matrix = json.loads(path.read_text())
        matrix["rows"].pop()
        path.write_text(json.dumps(matrix, sort_keys=True, indent=2) + "\n")
        with self.assertRaisesRegex(CompatibilityError, "surface inventory"):
            verify(root, expected_source_ref=SOURCE_REF)

    def test_missing_evidence_node_is_rejected(self) -> None:
        root = self._copy()
        path = root / "contracts/compatibility/trust-matrix-v1.json"
        matrix = json.loads(path.read_text())
        matrix["rows"][0]["evidence"] = "core/api/db.py::does_not_exist"
        path.write_text(json.dumps(matrix, sort_keys=True, indent=2) + "\n")
        with self.assertRaisesRegex(CompatibilityError, "evidence symbol missing"):
            verify(root, expected_source_ref=SOURCE_REF)

    def test_projection_payload_digest_is_required(self) -> None:
        root = self._copy()
        path = root / "contracts/engine-pin.yaml"
        path.write_text(
            path.read_text(encoding="utf-8").replace(PAYLOAD_SHA256, "not-a-digest"),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(CompatibilityError, "projection payload digest"):
            verify(root, expected_source_ref=SOURCE_REF)


if __name__ == "__main__":
    unittest.main()
