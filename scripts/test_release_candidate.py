from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest import mock
import zipfile

import release_candidate as candidate


ROOT = Path(__file__).resolve().parents[1]


class ReleaseCandidateTests(unittest.TestCase):
    def policy(self) -> dict:
        return copy.deepcopy(candidate.load_policy(ROOT))

    def write_manifest(self, path: Path, **values: object) -> dict:
        manifest = {"schema": candidate.MANIFEST_SCHEMA, **values}
        manifest["content_digest"] = candidate._sha_bytes(candidate._canonical(manifest))
        path.write_text(json.dumps(manifest), encoding="utf-8")
        return manifest

    def write_release_artifacts(self, dist: Path) -> tuple[Path, Path]:
        dist.mkdir(parents=True, exist_ok=True)
        metadata = b"Metadata-Version: 2.1\nName: marvisx-cli\nVersion: 0.4.1\n\n"
        wheel = dist / "marvisx_cli-0.4.1-py3-none-any.whl"
        with zipfile.ZipFile(wheel, "w") as archive:
            archive.writestr("marvisx_cli-0.4.1.dist-info/METADATA", metadata)
        sdist = dist / "marvisx_cli-0.4.1.tar.gz"
        with tarfile.open(sdist, "w:gz") as archive:
            info = tarfile.TarInfo("marvisx_cli-0.4.1/PKG-INFO")
            info.size = len(metadata)
            archive.addfile(info, io.BytesIO(metadata))
        return wheel, sdist

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
            "verified_at": now.isoformat(),
            "verified_by": "owner",
            "repository": policy["repository"],
            "workflow": policy["trusted_publisher"]["workflow"],
            "environment": policy["github_environment"]["name"],
            "candidate_tag": policy["candidate_tag"],
            "approval_deadline_hours": policy["approval_deadline_hours"],
            "late_approval_upload_guard": True,
        }
        return policy

    def test_real_release_candidate_static_policy(self) -> None:
        report = candidate.validate_static(ROOT)
        self.assertEqual(report["status"], "static_green_external_gates_open")
        self.assertEqual(report["version"], "0.4.1")
        self.assertEqual(report["release_branch"], "main")
        self.assertEqual(len(report["action_pins"]), 5)

    def test_unpinned_action_is_rejected(self) -> None:
        with self.assertRaisesRegex(candidate.ReleasePolicyError, "full commit"):
            candidate._action_pins("steps:\n  - uses: actions/checkout@v4\n")

    def test_commented_workflow_command_is_not_active_evidence(self) -> None:
        lines = candidate._active_workflow_lines(
            "# python scripts/test_release_candidate.py\nrun: echo ok\n"
        )
        self.assertNotIn("python scripts/test_release_candidate.py", lines)
        self.assertIn("run: echo ok", lines)

    def test_tag_build_rejects_another_trigger_tag(self) -> None:
        with self.assertRaisesRegex(candidate.ReleasePolicyError, "another-tag"):
            candidate._validate_tag_trigger("v0.4.1", "refs/tags/another-tag")

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

    def test_watchdog_for_another_tag_is_rejected(self) -> None:
        now = datetime(2026, 8, 28, 10, 0, tzinfo=timezone.utc)
        policy = self.verified_external_policy(now)
        policy["approval_watchdog"]["candidate_tag"] = "v0.4.2"
        with self.assertRaisesRegex(candidate.ReleasePolicyError, "coordinates"):
            candidate._strict_external_receipts(policy, now=now)

    def test_manifest_byte_tamper_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory(prefix="release-manifest-") as raw:
            dist = Path(raw) / "dist"
            artifact, _ = self.write_release_artifacts(dist)
            manifest = candidate.build_manifest(ROOT, dist)
            manifest_path = Path(raw) / "manifest.json"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            artifact.write_bytes(b"tampered")
            with self.assertRaisesRegex(candidate.ReleasePolicyError, "bytes differ"):
                candidate.verify_manifest(ROOT, manifest_path, dist)

    def test_manifest_recomputed_identity_tamper_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory(prefix="release-identity-") as raw:
            dist = Path(raw) / "dist"
            self.write_release_artifacts(dist)
            manifest = candidate.build_manifest(ROOT, dist)
            manifest["tag"] = "v9.9.9"
            manifest.pop("content_digest")
            manifest["content_digest"] = candidate._sha_bytes(
                candidate._canonical(manifest)
            )
            manifest_path = Path(raw) / "manifest.json"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaisesRegex(candidate.ReleasePolicyError, "identity differs"):
                candidate.verify_manifest(ROOT, manifest_path, dist)

    def test_manifest_source_must_be_the_checked_out_commit(self) -> None:
        with tempfile.TemporaryDirectory(prefix="release-source-") as raw:
            dist = Path(raw) / "dist"
            self.write_release_artifacts(dist)
            base = self.policy()["plan_b_product_base_sha"]
            with self.assertRaisesRegex(candidate.ReleasePolicyError, "checked-out"):
                candidate.build_manifest(ROOT, dist, source_sha=base)

    def test_sdist_uses_root_metadata_and_ignores_egg_info_copy(self) -> None:
        metadata = b"Name: marvisx-cli\nVersion: 0.4.1\n\n"
        with tempfile.TemporaryDirectory(prefix="release-sdist-") as raw:
            archive_path = Path(raw) / "marvisx_cli-0.4.1.tar.gz"
            with tarfile.open(archive_path, "w:gz") as archive:
                for name in (
                    "marvisx_cli-0.4.1/PKG-INFO",
                    "marvisx_cli-0.4.1/marvisx_cli.egg-info/PKG-INFO",
                ):
                    info = tarfile.TarInfo(name)
                    info.size = len(metadata)
                    archive.addfile(info, io.BytesIO(metadata))
            self.assertEqual(
                candidate._distribution_metadata(archive_path),
                ("marvisx-cli", "0.4.1"),
            )

    def test_manifest_rejects_unmanifested_release_asset(self) -> None:
        with tempfile.TemporaryDirectory(prefix="release-assets-") as raw:
            dist = Path(raw) / "dist"
            self.write_release_artifacts(dist)
            manifest_path = dist / "release-manifest.json"
            manifest = candidate.build_manifest(ROOT, dist)
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            (dist / "unreviewed-installer.sh").write_text("exit 0\n", encoding="utf-8")
            with self.assertRaisesRegex(candidate.ReleasePolicyError, "file set"):
                candidate.verify_manifest(ROOT, manifest_path, dist)

    def test_registry_readback_retries_then_matches_exact_bytes(self) -> None:
        with tempfile.TemporaryDirectory(prefix="registry-readback-") as raw:
            manifest_path = Path(raw) / "manifest.json"
            self.write_manifest(
                manifest_path,
                package="marvisx-cli",
                version="0.4.1",
                artifacts=[{"filename": "a.whl", "size": 7, "sha256": "a" * 64}],
            )
            payload = {
                "info": {"name": "marvisx-cli", "version": "0.4.1"},
                "urls": [
                    {
                        "filename": "a.whl",
                        "size": 7,
                        "digests": {"sha256": "a" * 64},
                        "yanked": False,
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

    def test_registry_readback_rejects_yanked_candidate(self) -> None:
        with tempfile.TemporaryDirectory(prefix="registry-yanked-") as raw:
            manifest_path = Path(raw) / "manifest.json"
            self.write_manifest(
                manifest_path,
                package="marvisx-cli",
                version="0.4.1",
                artifacts=[{"filename": "a.whl", "size": 7, "sha256": "a" * 64}],
            )
            payload = {
                "info": {"name": "marvisx-cli", "version": "0.4.1"},
                "urls": [
                    {
                        "filename": "a.whl",
                        "size": 7,
                        "digests": {"sha256": "a" * 64},
                        "yanked": True,
                    }
                ],
            }
            with mock.patch.object(candidate, "_request_json", return_value=payload):
                with self.assertRaisesRegex(candidate.ReleasePolicyError, "readback"):
                    candidate.registry_verify(
                        manifest_path, attempts=1, delay_seconds=0
                    )

    def test_registry_download_writes_only_verified_pythonhosted_bytes(self) -> None:
        raw_artifact = b"payload"
        sha256 = candidate._sha_bytes(raw_artifact)
        url = "https://files.pythonhosted.org/packages/a/a.whl"
        with tempfile.TemporaryDirectory(prefix="registry-download-") as raw:
            root = Path(raw)
            manifest_path = root / "manifest.json"
            self.write_manifest(
                manifest_path,
                package="marvisx-cli",
                version="0.4.1",
                artifacts=[
                    {"filename": "a.whl", "size": len(raw_artifact), "sha256": sha256}
                ],
            )
            payload = {
                "info": {"name": "marvisx-cli", "version": "0.4.1"},
                "urls": [
                    {
                        "filename": "a.whl",
                        "size": len(raw_artifact),
                        "digests": {"sha256": sha256},
                        "yanked": False,
                        "url": url,
                    }
                ],
            }
            response = mock.MagicMock()
            response.__enter__.return_value = response
            response.read.return_value = raw_artifact
            response.geturl.return_value = url
            destination = root / "download"
            with mock.patch.object(
                candidate, "_request_json", return_value=payload
            ), mock.patch.object(
                candidate.urllib.request, "urlopen", return_value=response
            ):
                report = candidate.registry_download(
                    manifest_path, destination, attempts=1, delay_seconds=0
                )
            self.assertEqual(report["status"], "registry_downloaded")
            self.assertEqual((destination / "a.whl").read_bytes(), raw_artifact)

    def test_registry_download_rejects_off_host_url(self) -> None:
        raw_artifact = b"payload"
        sha256 = candidate._sha_bytes(raw_artifact)
        with tempfile.TemporaryDirectory(prefix="registry-off-host-") as raw:
            root = Path(raw)
            manifest_path = root / "manifest.json"
            self.write_manifest(
                manifest_path,
                package="marvisx-cli",
                version="0.4.1",
                artifacts=[
                    {"filename": "a.whl", "size": len(raw_artifact), "sha256": sha256}
                ],
            )
            payload = {
                "info": {"name": "marvisx-cli", "version": "0.4.1"},
                "urls": [
                    {
                        "filename": "a.whl",
                        "size": len(raw_artifact),
                        "digests": {"sha256": sha256},
                        "yanked": False,
                        "url": "https://example.invalid/a.whl",
                    }
                ],
            }
            with mock.patch.object(candidate, "_request_json", return_value=payload):
                with self.assertRaisesRegex(candidate.ReleasePolicyError, "canonical"):
                    candidate.registry_download(
                        manifest_path, root / "download", attempts=1, delay_seconds=0
                    )

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

    def test_tag_preflight_requires_unused_release_and_pypi_namespaces(self) -> None:
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
            candidate, "_remote_tag_sha", return_value=reviewed
        ), mock.patch.object(candidate, "_require_remote_absence") as absence:
            report = candidate.tag_preflight(
                ROOT, token="masked", api_url="https://api.example"
            )
        self.assertEqual(report["status"], "tag_preflight_green")
        self.assertEqual(absence.call_count, 2)

    def test_preflight_rejects_source_that_is_not_remote_main_head(self) -> None:
        policy = self.verified_external_policy(
            datetime(2026, 8, 28, 10, 0, tzinfo=timezone.utc)
        )
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
            candidate,
            "_request_json",
            return_value={"commit": {"sha": "b" * 40}},
        ):
            with self.assertRaisesRegex(candidate.ReleasePolicyError, "exact remote"):
                candidate.preflight(ROOT, token="masked", api_url="https://api.example")

    def test_preflight_proves_exact_head_and_unused_namespace(self) -> None:
        policy = self.verified_external_policy(
            datetime(2026, 8, 28, 10, 0, tzinfo=timezone.utc)
        )
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
            candidate,
            "_request_json",
            side_effect=[
                {"commit": {"sha": reviewed}},
                FileNotFoundError("tag"),
                FileNotFoundError("release"),
                FileNotFoundError("pypi"),
            ],
        ):
            report = candidate.preflight(
                ROOT, token="masked", api_url="https://api.example"
            )
        self.assertEqual(report["status"], "preflight_green")
        self.assertEqual(
            report["namespace"],
            {
                "git_tag": "absent",
                "github_release": "absent",
                "pypi_version": "absent",
            },
        )

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
            self.write_manifest(manifest_path, release_source_sha="a" * 40)
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

    def test_publication_window_rejects_another_tag_run(self) -> None:
        now = datetime(2026, 8, 28, 10, 0, tzinfo=timezone.utc)
        policy = self.verified_external_policy(now)
        with tempfile.TemporaryDirectory(prefix="approval-tag-") as raw:
            manifest_path = Path(raw) / "manifest.json"
            self.write_manifest(manifest_path, release_source_sha="a" * 40)
            run = {
                "created_at": now.isoformat(),
                "head_sha": "a" * 40,
                "path": str(candidate.WORKFLOW_PATH),
                "event": "push",
                "head_branch": "v0.4.2",
                "head_repository": {"full_name": policy["repository"]},
            }
            with mock.patch.object(
                candidate, "load_policy", return_value=policy
            ), mock.patch.object(
                candidate, "_request_json", return_value=run
            ):
                with self.assertRaisesRegex(candidate.ReleasePolicyError, "another tag"):
                    candidate.publish_window(
                        ROOT,
                        manifest_path,
                        Path(raw),
                        token="masked",
                        run_id="123",
                        now=now,
                        api_url="https://api.example",
                    )

    def test_publication_window_rechecks_pypi_absence_before_upload(self) -> None:
        now = datetime(2026, 8, 28, 10, 0, tzinfo=timezone.utc)
        policy = self.verified_external_policy(now)
        with tempfile.TemporaryDirectory(prefix="approval-pypi-") as raw:
            manifest_path = Path(raw) / "manifest.json"
            self.write_manifest(manifest_path, release_source_sha="a" * 40)
            run = {
                "created_at": now.isoformat(),
                "head_sha": "a" * 40,
                "path": str(candidate.WORKFLOW_PATH),
                "event": "push",
                "head_branch": policy["candidate_tag"],
                "head_repository": {"full_name": policy["repository"]},
            }
            with mock.patch.object(
                candidate, "load_policy", return_value=policy
            ), mock.patch.object(
                candidate, "_request_json", return_value=run
            ), mock.patch.object(
                candidate, "_remote_tag_sha", return_value="a" * 40
            ), mock.patch.object(candidate, "_draft_release"), mock.patch.object(
                candidate, "_environment_check", return_value={"name": "pypi"}
            ), mock.patch.object(candidate, "verify_manifest"), mock.patch.object(
                candidate,
                "_require_remote_absence",
                side_effect=candidate.ReleasePolicyError("candidate PyPI version already exists"),
            ):
                with self.assertRaisesRegex(candidate.ReleasePolicyError, "already exists"):
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
