from __future__ import annotations

from email.message import Message
from pathlib import Path
import tempfile
import unittest
from zipfile import ZIP_DEFLATED, ZipFile

from verify_public_claims import (
    PublicClaimError,
    _check_text,
    verify_artifact,
    verify_source,
)


ROOT = Path(__file__).resolve().parents[1]


class PublicClaimTests(unittest.TestCase):
    def test_current_source_passes(self) -> None:
        result = verify_source(ROOT)
        self.assertEqual(result["license"], "BSL-1.1")

    def test_repository_slug_is_not_product_copy(self) -> None:
        self.assertEqual(_check_text("README.md", "emiliomartucci/marvisx-oss"), [])

    def test_unqualified_audit_claim_is_rejected(self) -> None:
        violations = _check_text("README.md", "A tamper-evident audit log.")
        self.assertEqual(violations[0].rule, "unqualified audit claim")

    def test_inaccurate_license_alias_is_rejected(self) -> None:
        violations = _check_text("METADATA", "MarvisX OSS runtime")
        self.assertEqual(violations[0].rule, "inaccurate license claim")

    def test_inaccurate_telemetry_default_is_rejected(self) -> None:
        for copy in (
            "Anonymous telemetry is opt-out.",
            "Telemetry is default-on.",
            "default → on",
        ):
            with self.subTest(copy=copy):
                violations = _check_text("public-copy", copy)
                self.assertEqual(violations[0].rule, "inaccurate telemetry default")

    def test_built_metadata_is_checked(self) -> None:
        with tempfile.TemporaryDirectory(prefix="marvis-claims-") as raw:
            wheel = Path(raw) / "sample-1.0-py3-none-any.whl"
            metadata = Message()
            metadata["Metadata-Version"] = "2.4"
            metadata["Name"] = "sample"
            metadata["Version"] = "1.0"
            metadata.set_payload("A tamper-evident audit log.")
            with ZipFile(wheel, "w", compression=ZIP_DEFLATED) as archive:
                archive.writestr("sample-1.0.dist-info/METADATA", metadata.as_string())
            with self.assertRaisesRegex(PublicClaimError, "audit claim"):
                verify_artifact(wheel)

    def test_built_metadata_requires_bsl_license(self) -> None:
        with tempfile.TemporaryDirectory(prefix="marvis-claims-") as raw:
            wheel = Path(raw) / "sample-1.0-py3-none-any.whl"
            metadata = Message()
            metadata["Metadata-Version"] = "2.4"
            metadata["Name"] = "sample"
            metadata["Version"] = "1.0"
            metadata["License"] = "Apache-2.0"
            metadata.set_payload("A source-available local runtime.")
            with ZipFile(wheel, "w", compression=ZIP_DEFLATED) as archive:
                archive.writestr("sample-1.0.dist-info/METADATA", metadata.as_string())
            with self.assertRaisesRegex(PublicClaimError, "package license"):
                verify_artifact(wheel)

    def test_built_metadata_accepts_bsl_license(self) -> None:
        with tempfile.TemporaryDirectory(prefix="marvis-claims-") as raw:
            wheel = Path(raw) / "sample-1.0-py3-none-any.whl"
            metadata = Message()
            metadata["Metadata-Version"] = "2.4"
            metadata["Name"] = "sample"
            metadata["Version"] = "1.0"
            metadata["License"] = "BSL-1.1"
            metadata.set_payload("A source-available local runtime.")
            with ZipFile(wheel, "w", compression=ZIP_DEFLATED) as archive:
                archive.writestr("sample-1.0.dist-info/METADATA", metadata.as_string())
            self.assertEqual(verify_artifact(wheel)["license"], "BSL-1.1")


if __name__ == "__main__":
    unittest.main()
