from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import unittest

from verify_compatibility_contract import CompatibilityError, provider_breaks, verify


ROOT = Path(__file__).resolve().parents[1]
SOURCE_REF = "962e0fa6bb15ca71e3b20e9c99636aa93c631271"
PAYLOAD_SHA256 = "6f1b5667df2b0a0ec91a2508914761e12c209f622557d7d8b912008849351717"


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
            "core/api/mcp/tools/projects.py",
            "core/api/models/auth.py",
            "core/api/models/tasks.py",
            "core/api/routers/agent_tokens.py",
            "core/api/routers/auth.py",
            "core/api/routers/graph_ingest.py",
            "core/api/routers/projects.py",
            "core/api/routers/tasks.py",
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

    @staticmethod
    def _rewrite_fixture(root: Path, name: str, value: dict) -> str:
        fixtures = root / "contracts/compatibility/fixtures"
        path = fixtures / name
        path.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        manifest_path = fixtures / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["files"][name] = digest
        manifest_path.write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n")
        return digest

    def test_current_contract_passes(self) -> None:
        result = verify(ROOT, expected_source_ref=SOURCE_REF)
        self.assertEqual(result["source_ref"], SOURCE_REF)
        self.assertEqual(result["projection_payload_sha256"], PAYLOAD_SHA256)
        self.assertGreater(result["n_operations"], result["n_minus_1_operations"])
        self.assertEqual(result["declared_n_minus_1_schema_breaks"], 4)

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
        new_digest = hashlib.sha256(
            broken_path.read_bytes()
        ).hexdigest()
        manifest["files"]["deliberate-break.json"] = new_digest
        manifest_path.write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n")
        pin_path = root / "contracts/engine-pin.yaml"
        old_pin = next(
            line.split(":", 1)[1].strip()
            for line in pin_path.read_text().splitlines()
            if line.startswith("deliberate_break_fixture_sha256:")
        )
        pin_path.write_text(pin_path.read_text().replace(old_pin, new_digest))
        with self.assertRaisesRegex(CompatibilityError, "does not reconstruct"):
            verify(root, expected_source_ref=SOURCE_REF)

    def test_current_openapi_must_reconstruct_the_committed_fixture(self) -> None:
        root = self._copy()
        openapi_path = root / "contracts/openapi/marvisx.json"
        spec = json.loads(openapi_path.read_text())
        first_path = sorted(spec["paths"])[0]
        first_method = next(
            method
            for method in ("delete", "get", "head", "options", "patch", "post", "put")
            if method in spec["paths"][first_path]
        )
        del spec["paths"][first_path][first_method]
        openapi_path.write_text(json.dumps(spec, sort_keys=True, indent=2) + "\n")
        fixture_path = root / "contracts/compatibility/fixtures/n-contract.json"
        fixture = json.loads(fixture_path.read_text())
        fixture["source_openapi_sha256"] = hashlib.sha256(
            openapi_path.read_bytes()
        ).hexdigest()
        self._rewrite_fixture(root, "n-contract.json", fixture)

        with self.assertRaisesRegex(CompatibilityError, "does not reconstruct"):
            verify(root, expected_source_ref=SOURCE_REF)

    def test_n_minus_one_identity_cannot_be_self_relabelled(self) -> None:
        root = self._copy()
        fixtures = root / "contracts/compatibility/fixtures"
        previous = json.loads((fixtures / "n-minus-1-contract.json").read_text())
        previous["source_ref"] = "f" * 40
        self._rewrite_fixture(root, "n-minus-1-contract.json", previous)

        with self.assertRaisesRegex(CompatibilityError, "external pin"):
            verify(root, expected_source_ref=SOURCE_REF)

    def test_consumer_view_detects_referenced_schema_type_change(self) -> None:
        operation = {
            "operation_id": "get_item",
            "required_parameters": [],
            "request_body_required": False,
            "response_codes": ["200"],
            "request_schema_refs": [],
            "response_schema_refs": {"200": ["Item"]},
            "schema_refs": ["Item"],
        }
        shape = {
            "kind": "object",
            "envelope_sha256": "a" * 64,
            "required": ["value"],
            "property_sha256": {"value": "b" * 64},
        }
        expected = {
            "operations": {"/items": {"get": operation}},
            "component_schema_compatibility": {"Item": shape},
        }
        changed = json.loads(json.dumps(expected))
        changed["component_schema_compatibility"]["Item"]["property_sha256"]["value"] = "c" * 64

        failures = provider_breaks(expected, changed, consumer_view=True)

        self.assertIn("schema_property_changed:Item.value", failures)

    def test_consumer_view_checks_dual_use_schema_as_response(self) -> None:
        operation = {
            "operation_id": "replace_item",
            "required_parameters": [],
            "request_body_required": True,
            "response_codes": ["200"],
            "request_schema_refs": ["Item"],
            "response_schema_refs": {"200": ["Item"]},
            "schema_refs": ["Item"],
        }
        shape = {
            "kind": "object",
            "envelope_sha256": "a" * 64,
            "required": ["value"],
            "property_sha256": {"value": "b" * 64},
        }
        expected = {
            "operations": {"/items": {"put": operation}},
            "component_schema_compatibility": {"Item": shape},
        }
        changed = json.loads(json.dumps(expected))
        changed["component_schema_compatibility"]["Item"]["required"] = []

        failures = provider_breaks(expected, changed, consumer_view=True)

        self.assertIn("schema_required_response_missing:Item", failures)

    def test_n_minus_one_schema_type_change_cannot_be_self_certified(self) -> None:
        root = self._copy()
        fixtures = root / "contracts/compatibility/fixtures"
        previous = json.loads((fixtures / "n-minus-1-contract.json").read_text())
        previous["component_schema_compatibility"]["TaskUpdateRequest"][
            "property_sha256"
        ]["status"] = "f" * 64
        self._rewrite_fixture(root, "n-minus-1-contract.json", previous)

        with self.assertRaisesRegex(CompatibilityError, "does not reconstruct"):
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

    def test_n_client_against_n_minus_one_mcp_requires_migration(self) -> None:
        root = self._copy()
        path = root / "contracts/compatibility/consumer-matrix-v1.json"
        matrix = json.loads(path.read_text())
        local_mcp = next(
            row for row in matrix["rows"] if row["surface"] == "local_mcp"
        )
        local_mcp["n_consumer_n_minus_1_contract"] = "pass"
        path.write_text(json.dumps(matrix, sort_keys=True, indent=2) + "\n")

        with self.assertRaisesRegex(CompatibilityError, "N-only MCP tool"):
            verify(root, expected_source_ref=SOURCE_REF)

    def test_current_mcp_inventory_cannot_be_self_certified(self) -> None:
        root = self._copy()
        path = root / "contracts/compatibility/fixtures/n-mcp-tools.json"
        inventory = json.loads(path.read_text())
        inventory["tools"].pop()
        inventory["tool_count"] = len(inventory["tools"])
        self._rewrite_fixture(root, "n-mcp-tools.json", inventory)

        with self.assertRaisesRegex(CompatibilityError, "does not reconstruct"):
            verify(root, expected_source_ref=SOURCE_REF)


if __name__ == "__main__":
    unittest.main()
