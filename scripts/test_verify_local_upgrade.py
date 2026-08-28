from __future__ import annotations

import hashlib
from pathlib import Path
import tempfile
import unittest
import zipfile

from verify_local_upgrade import (
    PriorDistribution,
    UpgradeVerificationError,
    _assert_invariants,
    verify_upgrade,
)


ROOT = Path(__file__).resolve().parents[1]


class LocalUpgradeTests(unittest.TestCase):
    def _prior_wheel(self, directory: Path) -> tuple[Path, PriorDistribution]:
        from core.api import db as db_mod

        files = db_mod.discover_up_migrations(ROOT / "migrations")
        maximum = db_mod.code_max_version(files)
        selected = [path for path in files if db_mod._migration_version(path) < maximum]
        wheel = directory / "marvisx_cli-test-prior-py3-none-any.whl"
        with zipfile.ZipFile(wheel, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for path in selected:
                archive.write(path, f"migrations/{path.name}")
            archive.writestr(
                "marvisx_cli-test_prior.dist-info/METADATA",
                "Metadata-Version: 2.1\nName: marvisx-cli\nVersion: test-prior\n",
            )
        digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
        return wheel, PriorDistribution(
            version="test-prior",
            role="test",
            filename=wheel.name,
            sha256=digest,
            url="https://invalid.example/test.whl",
        )

    def test_synthetic_prior_upgrade_and_rollback(self) -> None:
        with tempfile.TemporaryDirectory(prefix="marvis-upgrade-test-") as raw:
            wheel, prior = self._prior_wheel(Path(raw))
            result = verify_upgrade(ROOT, wheel, prior)
        self.assertEqual(result["old_binary_forward_schema"], "deny")
        self.assertEqual(result["rollback_status"], "rolled_back")
        self.assertTrue(result["rollback_logical_digest_restored"])

    def test_invariant_surface_tamper_fails_closed(self) -> None:
        with self.assertRaisesRegex(UpgradeVerificationError, "surface mutated"):
            _assert_invariants({"settings": "a"}, {"settings": "b"})


if __name__ == "__main__":
    unittest.main()
