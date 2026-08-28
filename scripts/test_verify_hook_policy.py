from __future__ import annotations

from pathlib import Path
import shutil
import tempfile
import unittest

from generate_hook_policy import HookGenerationError, generate
from verify_hook_policy import verify


ROOT = Path(__file__).resolve().parents[1]


class HookPolicyTests(unittest.TestCase):
    def test_five_representation_policy_passes(self) -> None:
        result = verify(ROOT)
        self.assertEqual(result["behavior_cases"], 3)
        self.assertEqual(result["dependency_failures"], 5)

    def test_generated_resource_drift_fails(self) -> None:
        with tempfile.TemporaryDirectory(prefix="marvis-hook-policy-") as raw:
            root = Path(raw)
            for relative in (
                "contracts/hooks/policy-v1.json",
                "core/scripts/safety_bridge.py",
                "core/scripts/install_hooks/safety_bridge.py",
            ):
                target = root / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(ROOT / relative, target)
            path = root / "core/scripts/install_hooks/safety_bridge.py"
            path.write_bytes(path.read_bytes() + b"# drift\n")
            with self.assertRaisesRegex(HookGenerationError, "drifted"):
                generate(root, write=False)


if __name__ == "__main__":
    unittest.main()
