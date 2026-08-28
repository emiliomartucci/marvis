from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import release_candidate as candidate


ROOT = Path(__file__).resolve().parents[1]


class ReleaseCandidateTests(unittest.TestCase):
    def policy(self) -> dict:
        return copy.deepcopy(candidate.load_policy(ROOT))

    def verified_external_policy(self, now: datetime) -> dict:
        policy = self.policy()
        policy["trusted_publisher"]["readback"] = {
            "status": "verified",
            "verified_at": now.isoformat(),
            "verified_by": "owner",
            "coordinates_sha256": candidate.publisher_coordinates_sha256(policy),
        }
        policy["approval_watchdog"] = {
            "status": "ready",
            "receipt_ref": "ledger:receipt",
            "late_approval_upload_guard": True,
        }
        return policy

    def test_real_release_candidate_static_policy(self) -> None:
        report = candidate.validate_static(ROOT)
        self.assertEqual(report["status"], "static_green_external_gates_open")
        self.assertEqual(report["version"], "0.4.1")
        self.assertEqual(len(report["action_pins"]), 5)

    def test_unpinned_action_is_rejected(self) -> None:
        with self.assertRaisesRegex(candidate.ReleasePolicyError, "full commit"):
            candidate._action_pins("steps:\n  - uses: actions/checkout@v4\n")

    def test_release_delta_rejects_product_code(self) -> None:
        with mock.patch.object(
            candidate, "_changed_paths", return_value=["core/api/main.py"]
        ):
            with self.assertRaisesRegex(candidate.ReleasePolicyError, "product behavior"):
                candidate.validate_static(ROOT)

    def test_external_receipts_fail_closed(self) -> None:
        with self.assertRaisesRegex(candidate.ReleasePolicyError, "not verified"):
            candidate._strict_external_receipts(self.policy())

    def test_external_receipt_expires_after_24_hours(self) -> None:
        now = datetime(2026, 8, 28, 10, 0, tzinfo=timezone.utc)
        policy = self.verified_external_policy(now - timedelta(hours=25))
        with self.assertRaisesRegex(candidate.ReleasePolicyError, "stale"):
            candidate._strict_external_receipts(policy, now=now)

    def test_fresh_matching_external_receipts_pass(self) -> None:
        now = datetime(2026, 8, 28, 10, 0, tzinfo=timezone.utc)
        candidate._strict_external_receipts(
            self.verified_external_policy(now - timedelta(minutes=5)), now=now
        )

    def test_manifest_byte_tamper_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory(prefix="release-manifest-") as raw:
            root = Path(raw)
            (root / ".github/workflows").mkdir(parents=True)
            (root / "contracts/release").mkdir(parents=True)
            workflow = root / candidate.WORKFLOW_PATH
            policy_path = root / candidate.POLICY_PATH
            workflow.write_bytes((ROOT / candidate.WORKFLOW_PATH).read_bytes())
            policy_path.write_bytes((ROOT / candidate.POLICY_PATH).read_bytes())
            dist = root / "dist"
            dist.mkdir()
            artifact = dist / "candidate.whl"
            artifact.write_bytes(b"reviewed")
            manifest = {
                "schema": candidate.MANIFEST_SCHEMA,
                "workflow": {"sha256": candidate._sha_file(workflow)},
                "policy": {"sha256": candidate._sha_file(policy_path)},
                "artifacts": [
                    {
                        "filename": artifact.name,
                        "size": artifact.stat().st_size,
                        "sha256": candidate._sha_file(artifact),
                    }
                ],
            }
            manifest["content_digest"] = candidate._sha_bytes(
                candidate._canonical(manifest)
            )
            manifest_path = root / "manifest.json"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            artifact.write_bytes(b"tampered")
            with self.assertRaisesRegex(candidate.ReleasePolicyError, "bytes differ"):
                candidate.verify_manifest(root, manifest_path, dist)

    def test_registry_readback_retries_then_matches_exact_bytes(self) -> None:
        with tempfile.TemporaryDirectory(prefix="registry-readback-") as raw:
            manifest_path = Path(raw) / "manifest.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "package": "marvisx-cli",
                        "version": "0.4.1",
                        "artifacts": [
                            {"filename": "a.whl", "size": 7, "sha256": "abc"}
                        ],
                    }
                ),
                encoding="utf-8",
            )
            payload = {
                "urls": [
                    {
                        "filename": "a.whl",
                        "size": 7,
                        "digests": {"sha256": "abc"},
                    }
                ]
            }
            with mock.patch.object(
                candidate,
                "_request_json",
                side_effect=[FileNotFoundError("pending"), payload],
            ), mock.patch.object(candidate.time, "sleep") as sleep:
                report = candidate.registry_verify(
                    manifest_path, attempts=2, delay_seconds=0.01
                )
            self.assertEqual(report["attempt"], 2)
            sleep.assert_called_once_with(0.01)

    def test_pretag_rejects_remote_tag_on_another_commit(self) -> None:
        policy = self.policy()
        reviewed = "a" * 40
        with mock.patch.object(
            candidate,
            "validate_static",
            return_value={"release_source_sha": reviewed},
        ), mock.patch.object(candidate, "load_policy", return_value=policy), mock.patch.object(
            candidate, "_strict_external_receipts"
        ), mock.patch.object(
            candidate, "_environment_check", return_value={"name": "pypi"}
        ), mock.patch.object(
            candidate, "_remote_tag_sha", return_value="b" * 40
        ):
            with self.assertRaisesRegex(candidate.ReleasePolicyError, "reviewed"):
                candidate.pretag(ROOT, token="masked")

    def test_environment_admin_bypass_is_rejected(self) -> None:
        policy = self.policy()
        response = {
            "name": "pypi",
            "can_admins_bypass": True,
            "protection_rules": [
                {
                    "type": "required_reviewers",
                    "prevent_self_review": False,
                    "reviewers": [
                        {"reviewer": {"login": "emiliomartucci"}}
                    ],
                }
            ],
        }
        with mock.patch.object(candidate, "_request_json", return_value=response):
            with self.assertRaisesRegex(candidate.ReleasePolicyError, "bypass"):
                candidate._environment_check(
                    policy, token="masked", api_url="https://api.example"
                )

    def test_publication_window_rejects_late_approval(self) -> None:
        now = datetime(2026, 8, 28, 10, 0, tzinfo=timezone.utc)
        policy = self.verified_external_policy(now)
        with tempfile.TemporaryDirectory(prefix="approval-window-") as raw:
            manifest_path = Path(raw) / "manifest.json"
            manifest_path.write_text(
                json.dumps({"release_source_sha": "a" * 40}), encoding="utf-8"
            )
            run = {
                "created_at": (now - timedelta(hours=25)).isoformat(),
                "head_sha": "a" * 40,
                "path": str(candidate.WORKFLOW_PATH),
                "event": "push",
            }
            with mock.patch.object(
                candidate, "load_policy", return_value=policy
            ), mock.patch.object(
                candidate, "_request_json", return_value=run
            ):
                with self.assertRaisesRegex(candidate.ReleasePolicyError, "expired"):
                    candidate.publish_window(
                        ROOT,
                        manifest_path,
                        Path(raw),
                        token="masked",
                        run_id="123",
                        now=now,
                        api_url="https://api.example",
                    )


if __name__ == "__main__":
    unittest.main()
