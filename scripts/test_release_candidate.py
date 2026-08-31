from __future__ import annotations

import copy
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
import re
import subprocess
import tarfile
import tempfile
import types
import unittest
from unittest import mock
import zipfile

import yaml

import release_candidate as candidate


ROOT = Path(__file__).resolve().parents[1]


class ReleaseCandidateTests(unittest.TestCase):
    RELEASE_SOURCE_SHA = "a" * 40

    def policy(self) -> dict:
        return copy.deepcopy(candidate.load_policy(ROOT))

    @contextmanager
    def active_candidate_for_unit_test(self):
        """Exercise artifact logic while the real candidate is fail-closed."""
        with mock.patch.object(
            candidate, "_candidate_state", return_value={"status": "active"}
        ), mock.patch.object(candidate, "_path_allowed", return_value=True):
            yield

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
        del now
        return self.policy()

    def external_receipts(
        self, now: datetime, policy: dict | None = None
    ) -> tuple[dict, dict]:
        policy = policy or self.policy()
        trusted = {
            "schema": candidate.EXTERNAL_RECEIPT_SCHEMA,
            "kind": "trusted_publisher_owner_readback",
            "status": "verified",
            "verified_at": now.isoformat(),
            "verified_by": "owner",
            "coordinates_sha256": candidate.publisher_coordinates_sha256(policy),
        }
        write_authority = {
            "schema": candidate.WATCHDOG_WRITE_AUTHORITY_SCHEMA,
            "status": "verified",
            "repository": policy["repository"],
            "canary_workflow": policy["approval_watchdog"][
                "write_authority_canary_workflow"
            ],
            "environment": policy["github_environment"]["name"],
            "target_head_sha": self.RELEASE_SOURCE_SHA,
            "nonce": "watchdog-20260830-a1b2c3d4",
            "verified_at": now.isoformat(),
            "verified_by": "github-user:emiliomartucci",
            "capabilities": {
                "reject_pending_deployment": {
                    "status": "verified",
                    "mode": "reject-pending-deployment",
                    "nonce": "watchdog-20260830-a1b2c3d4",
                    "run_id": 1001,
                    "run_url": (
                        "https://github.com/emiliomartucci/marvis/actions/runs/1001"
                    ),
                    "observed_state": "rejected",
                },
                "cancel_workflow_run": {
                    "status": "verified",
                    "mode": "cancel-workflow-run",
                    "nonce": "watchdog-20260830-a1b2c3d4",
                    "run_id": 1002,
                    "run_url": (
                        "https://github.com/emiliomartucci/marvis/actions/runs/1002"
                    ),
                    "observed_conclusion": "cancelled",
                },
            },
            "worker_attestation": {
                "algorithm": "HMAC-SHA256",
                "worker_version_id": "worker-canary-version-122",
                "signature": "a" * 64,
            },
        }
        watchdog = {
            "schema": candidate.EXTERNAL_RECEIPT_SCHEMA,
            "kind": "approval_watchdog",
            "status": "ready",
            "receipt_ref": (
                "cloudflare-worker://marvis-oss-release-watchdog-041/"
                f"worker-version-123/{'d' * 64}"
            ),
            "verified_at": now.isoformat(),
            "verified_by": "owner",
            "repository": policy["repository"],
            "workflow": policy["trusted_publisher"]["workflow"],
            "environment": policy["github_environment"]["name"],
            "candidate_tag": policy["candidate_tag"],
            "write_authority_canary_workflow": policy["approval_watchdog"][
                "write_authority_canary_workflow"
            ],
            "approval_deadline_hours": policy["approval_deadline_hours"],
            "late_approval_upload_guard": True,
            "target_head_sha": self.RELEASE_SOURCE_SHA,
            "active_until": (now + timedelta(hours=49)).isoformat(),
            "worker_version": {
                "id": "worker-version-123",
                "tag": "release-watchdog-041",
                "timestamp": now.isoformat(),
            },
            "write_authority": write_authority,
            "write_authority_sha256": candidate._sha_bytes(
                candidate._canonical(write_authority)
            ),
        }
        return trusted, watchdog

    def test_real_release_candidate_is_invalidated_by_the_source_advance(self) -> None:
        policy = self.policy()
        shared_source = candidate._shared_source_coordinates(ROOT, policy)
        state = candidate._candidate_state(policy, shared_source=shared_source)
        self.assertEqual(state["status"], "invalidated")
        self.assertEqual(
            state["invalidated_by_shared_source_sha"],
            "ab1fa58eae705a25ca46ea6829eb0d538794ee52",
        )
        with self.assertRaisesRegex(candidate.ReleasePolicyError, "invalidated"):
            candidate.validate_static(ROOT)

    def test_candidate_invalidation_cannot_name_an_unrelated_source(self) -> None:
        policy = self.policy()
        policy["candidate_state"] = {
            "status": "invalidated",
            "reason": "shared_source_advanced_after_release_foundation",
            "invalidated_by_shared_source_sha": "0" * 40,
            "required_next_gate": "merge_product_projection_then_refresh_release_foundation",
        }
        shared_source = candidate._shared_source_coordinates(ROOT, policy)
        with self.assertRaisesRegex(
            candidate.ReleasePolicyError, "invalidation evidence is inconsistent"
        ):
            candidate._candidate_state(policy, shared_source=shared_source)

    def test_active_candidate_cannot_keep_stale_invalidation_evidence(self) -> None:
        policy = self.policy()
        policy["candidate_state"]["status"] = "active"
        shared_source = candidate._shared_source_coordinates(ROOT, policy)
        with self.assertRaisesRegex(
            candidate.ReleasePolicyError, "active candidate state contains stale evidence"
        ):
            candidate._candidate_state(policy, shared_source=shared_source)

    def test_mutating_workflow_jobs_are_tag_only_and_privilege_minimal(self) -> None:
        workflow = (ROOT / candidate.WORKFLOW_PATH).read_text(encoding="utf-8")
        parsed = yaml.safe_load(workflow)
        guards = candidate._release_job_guards(workflow)
        expected = "${{ github.event_name == 'push' && startsWith(github.ref, 'refs/tags/v') }}"
        for job in ("release-record", "prepublish", "pypi", "accept", "finalize"):
            self.assertEqual(guards[job], expected)
            self.assertEqual(parsed["jobs"][job]["if"], expected)
        contain_guard = parsed["jobs"]["contain"]["if"]
        self.assertIn("github.event_name == 'push'", contain_guard)
        self.assertIn("startsWith(github.ref, 'refs/tags/v')", contain_guard)
        scenarios = {
            ("pull_request", "refs/pull/1/merge"): set(),
            ("workflow_dispatch", "refs/heads/main"): set(),
            ("push", "refs/tags/v0.4.1"): {
                "release-record",
                "prepublish",
                "pypi",
                "accept",
                "finalize",
                "contain",
            },
        }
        mutating = {
            "release-record",
            "prepublish",
            "pypi",
            "accept",
            "finalize",
            "contain",
        }
        for (event, ref), expected_jobs in scenarios.items():
            enabled = mutating if event == "push" and ref.startswith("refs/tags/v") else set()
            self.assertEqual(enabled, expected_jobs)
        blocks = candidate._workflow_job_blocks(workflow)
        for job in ("release-record", "pypi", "finalize"):
            self.assertNotIn("actions/checkout@", blocks[job])
            self.assertNotIn("scripts/", blocks[job])
            self.assertNotIn("pip install", blocks[job])
        self.assertIn("contents: read", blocks["accept"])
        self.assertNotIn("contents: write", blocks["accept"])

    def test_invalidated_pull_request_skips_every_expensive_release_step(self) -> None:
        workflow = yaml.safe_load((ROOT / candidate.WORKFLOW_PATH).read_text())
        steps = workflow["jobs"]["build"]["steps"]
        protected = {
            "Run the live pre-tag preflight before the expensive build",
            "Recheck the tagged namespace before the expensive build",
            "Build the local GUI from a digest-pinned image",
            "Fail if a foreign route reached the local artifact",
            "Build one wheel and one source archive",
            "Verify packaged claims, completeness and clean install",
            "Create and re-read the immutable artifact manifest",
            "Upload the only candidate artifact",
        }
        guarded = {step["name"] for step in steps if step.get("name") in protected}
        self.assertEqual(guarded, protected)
        for step in steps:
            if step.get("name") in protected:
                self.assertIn(
                    "steps.candidate.outputs.status == 'active'",
                    str(step.get("if") or ""),
                )

    def test_embedded_workflow_python_is_syntax_valid(self) -> None:
        workflow = (ROOT / candidate.WORKFLOW_PATH).read_text(encoding="utf-8")
        bodies = re.findall(r"<<'PY'\n(?P<body>.*?)\n\s*PY(?:\n|$)", workflow, re.DOTALL)
        self.assertGreaterEqual(len(bodies), 2)
        for index, body in enumerate(bodies, start=1):
            lines = body.splitlines()
            margin = min(len(line) - len(line.lstrip()) for line in lines if line.strip())
            source = "\n".join(line[margin:] for line in lines)
            compile(source, f"release.yml:python-heredoc-{index}", "exec")

    def test_release_build_validates_emitted_routes_before_packaging(self) -> None:
        workflow = yaml.safe_load(
            (ROOT / candidate.WORKFLOW_PATH).read_text(encoding="utf-8")
        )
        steps = workflow["jobs"]["build"]["steps"]
        names = [step.get("name") for step in steps]
        build_index = names.index("Build the local GUI from a digest-pinned image")
        gate_index = names.index("Fail if a foreign route reached the local artifact")
        package_index = names.index("Build one wheel and one source archive")
        self.assertLess(build_index, gate_index)
        self.assertLess(gate_index, package_index)
        self.assertEqual(
            steps[gate_index]["run"],
            "python scripts/validate_local_surfaces.py --bundle core/api/console_dist",
        )

    def test_shared_source_readback_requires_exact_pr_candidate_and_merge(self) -> None:
        policy = self.policy()
        expected = policy["shared_source"]
        pull = {
            "state": "closed",
            "merged_at": "2026-08-28T10:00:00Z",
            "head": {"sha": expected["candidate_sha"]},
            "merge_commit_sha": expected["merge_sha"],
        }
        with mock.patch.object(candidate, "_request_json", return_value=pull):
            report = candidate._shared_source_readback(
                ROOT, policy, token="masked", api_url="https://api.example"
            )
        self.assertEqual(report["candidate_sha"], expected["candidate_sha"])
        self.assertEqual(report["merge_sha"], expected["merge_sha"])

        pull["head"]["sha"] = "a" * 40
        with mock.patch.object(
            candidate, "_request_json", return_value=pull
        ), self.assertRaisesRegex(candidate.ReleasePolicyError, "identity"):
            candidate._shared_source_readback(
                ROOT, policy, token="masked", api_url="https://api.example"
            )

    def test_release_foundation_is_exact_and_remotely_read_back(self) -> None:
        policy = self.policy()
        expected = policy["release_foundation"]
        pull = {
            "state": "closed",
            "merged_at": "2026-08-29T15:01:11Z",
            "head": {"sha": expected["candidate_sha"]},
            "merge_commit_sha": expected["merge_sha"],
        }
        with mock.patch.object(candidate, "_request_json", return_value=pull):
            report = candidate._release_foundation_readback(
                ROOT, policy, token="masked", api_url="https://api.example"
            )
        self.assertEqual(report["pull_request"], 56)
        self.assertEqual(
            report["changed_paths"], expected["expected_changed_paths"]
        )

        pull["merge_commit_sha"] = "a" * 40
        with mock.patch.object(
            candidate, "_request_json", return_value=pull
        ), self.assertRaisesRegex(candidate.ReleasePolicyError, "identity"):
            candidate._release_foundation_readback(
                ROOT, policy, token="masked", api_url="https://api.example"
            )

    def test_release_foundation_rejects_unexpected_changed_path(self) -> None:
        policy = self.policy()
        policy["release_foundation"]["expected_changed_paths"] = sorted(
            [
                *policy["release_foundation"]["expected_changed_paths"],
                "scripts/unreviewed.py",
            ]
        )
        with self.assertRaisesRegex(candidate.ReleasePolicyError, "changed paths"):
            candidate._release_foundation_coordinates(ROOT, policy)

    def test_tag_requires_fresh_successful_dispatch_at_exact_sha(self) -> None:
        now = datetime(2026, 8, 28, 10, 0, tzinfo=timezone.utc)
        policy = self.policy()
        source = "a" * 40
        run = {
            "id": 42,
            "run_attempt": 1,
            "head_sha": source,
            "event": "workflow_dispatch",
            "conclusion": "success",
            "head_branch": "main",
            "path": str(candidate.WORKFLOW_PATH),
            "head_repository": {"full_name": policy["repository"]},
            "updated_at": (now - timedelta(minutes=5)).isoformat(),
            "html_url": "https://github.example/runs/42",
        }
        with mock.patch.object(
            candidate, "_request_json", return_value={"workflow_runs": [run]}
        ):
            proof = candidate._workflow_dispatch_proof(
                policy,
                source,
                token="masked",
                api_url="https://api.example",
                now=now,
            )
        self.assertEqual(proof["id"], 42)
        self.assertEqual(proof["head_sha"], source)

        run["head_sha"] = "b" * 40
        with mock.patch.object(
            candidate, "_request_json", return_value={"workflow_runs": [run]}
        ), self.assertRaisesRegex(candidate.ReleasePolicyError, "no fresh"):
            candidate._workflow_dispatch_proof(
                policy,
                source,
                token="masked",
                api_url="https://api.example",
                now=now,
            )

    def test_unpinned_action_is_rejected(self) -> None:
        with self.assertRaisesRegex(candidate.ReleasePolicyError, "full commit"):
            candidate._action_pins("steps:\n  - uses: actions/checkout@v4\n")

    def test_commented_workflow_command_is_not_active_evidence(self) -> None:
        lines = candidate._active_workflow_lines(
            "# python scripts/test_release_candidate.py\nrun: echo ok\n"
        )
        self.assertNotIn("python scripts/test_release_candidate.py", lines)
        self.assertIn("run: echo ok", lines)

    def test_release_source_tree_rejects_uncommitted_product_code(self) -> None:
        with mock.patch.object(
            candidate,
            "_git",
            side_effect=["M\tcore/api/main.py", "release-artifact/packages/a.whl"],
        ), self.assertRaisesRegex(candidate.ReleasePolicyError, "source tree"):
            candidate._require_release_controls_committed(
                ROOT,
                "a" * 40,
            )

    def test_release_source_tree_allows_only_untracked_generated_outputs(self) -> None:
        with mock.patch.object(
            candidate,
            "_git",
            side_effect=[
                "",
                "\n".join(
                    [
                        "build/lib/core/api/main.py",
                        "marvisx_cli.egg-info/PKG-INFO",
                        "release-artifact/packages/a.whl",
                    ]
                ),
            ],
        ):
            candidate._require_release_controls_committed(ROOT, "a" * 40)

    def test_release_source_tree_rejects_tracked_generated_path_changes(self) -> None:
        with mock.patch.object(
            candidate,
            "_git",
            side_effect=[
                "M\tcore/api/console_dist/index.html",
                "build/lib/generated.py",
            ],
        ), self.assertRaisesRegex(candidate.ReleasePolicyError, "source tree"):
            candidate._require_release_controls_committed(ROOT, "a" * 40)

    def test_release_source_tree_allows_only_deleted_console_placeholder(self) -> None:
        with mock.patch.object(
            candidate,
            "_git",
            side_effect=[
                "D\tcore/api/console_dist/.gitkeep",
                "core/api/console_dist/index.html",
            ],
        ):
            candidate._require_release_controls_committed(ROOT, "a" * 40)

        with mock.patch.object(
            candidate,
            "_git",
            side_effect=["M\tcore/api/console_dist/.gitkeep", ""],
        ), self.assertRaisesRegex(candidate.ReleasePolicyError, "source tree"):
            candidate._require_release_controls_committed(ROOT, "a" * 40)

    def test_tag_build_rejects_another_trigger_tag(self) -> None:
        with self.assertRaisesRegex(candidate.ReleasePolicyError, "another-tag"):
            candidate._validate_tag_trigger("v0.4.1", "refs/tags/another-tag")

    def test_release_delta_rejects_product_code(self) -> None:
        foundation = candidate._release_foundation_coordinates(ROOT, self.policy())
        with mock.patch.object(
            candidate, "_release_foundation_coordinates", return_value=foundation
        ), mock.patch.object(
            candidate, "_candidate_state", return_value={"status": "active"}
        ), mock.patch.object(candidate, "_changed_paths", return_value=["core/api/main.py"]):
            with self.assertRaisesRegex(candidate.ReleasePolicyError, "product behavior"):
                candidate.validate_static(ROOT)

    def test_release_source_must_descend_from_exact_foundation(self) -> None:
        with mock.patch.object(
            candidate, "_candidate_state", return_value={"status": "active"}
        ), self.assertRaisesRegex(candidate.ReleasePolicyError, "exact release foundation"):
            candidate.validate_static(ROOT, head=self.policy()["plan_b_product_base_sha"])

    def test_pyproject_release_delta_rejects_dependency_change(self) -> None:
        base = copy.deepcopy(candidate.tomllib.loads((ROOT / "pyproject.toml").read_text()))
        changed = copy.deepcopy(base)
        changed["project"]["dependencies"].append("unreviewed-runtime>=1")
        with mock.patch.object(
            candidate.tomllib,
            "loads",
            side_effect=[base, changed],
        ), self.assertRaisesRegex(candidate.ReleasePolicyError, "beyond project.version"):
            candidate._validate_pyproject_version_only(
                ROOT, self.policy()["release_foundation"]["merge_sha"]
            )

    def test_candidate_version_must_exceed_all_history(self) -> None:
        with self.assertRaisesRegex(candidate.ReleasePolicyError, "above all PyPI"):
            candidate._require_candidate_above_history(
                self.policy(), ["0.3.8", "0.4.2"], authority="PyPI"
            )

    def test_tagged_source_remains_valid_after_release_branch_advances(self) -> None:
        policy = self.policy()
        source = "a" * 40
        remote_head = "b" * 40
        comparison = {
            "status": "ahead",
            "merge_base_commit": {"sha": source},
        }
        with mock.patch.object(
            candidate, "_remote_release_branch_sha", return_value=remote_head
        ), mock.patch.object(candidate, "_request_json", return_value=comparison):
            observed = candidate._require_remote_release_source(
                policy,
                source,
                token="masked",
                api_url="https://api.example",
                allow_branch_advance=True,
            )
        self.assertEqual(observed, remote_head)

    def test_unmerged_tagged_source_is_rejected(self) -> None:
        policy = self.policy()
        source = "a" * 40
        with mock.patch.object(
            candidate, "_remote_release_branch_sha", return_value="b" * 40
        ), mock.patch.object(
            candidate,
            "_request_json",
            return_value={
                "status": "diverged",
                "merge_base_commit": {"sha": "c" * 40},
            },
        ), self.assertRaisesRegex(candidate.ReleasePolicyError, "not contained"):
            candidate._require_remote_release_source(
                policy,
                source,
                token="masked",
                api_url="https://api.example",
                allow_branch_advance=True,
            )

    def test_disconnected_tagged_source_is_rejected(self) -> None:
        with mock.patch.object(
            candidate, "_remote_release_branch_sha", return_value="b" * 40
        ), mock.patch.object(
            candidate, "_request_json", side_effect=FileNotFoundError("compare")
        ), self.assertRaisesRegex(candidate.ReleasePolicyError, "not contained"):
            candidate._require_remote_release_source(
                self.policy(),
                "a" * 40,
                token="masked",
                api_url="https://api.example",
                allow_branch_advance=True,
            )

    def test_external_receipts_fail_closed(self) -> None:
        with mock.patch.dict(candidate.os.environ, {}, clear=True), self.assertRaisesRegex(
            candidate.ReleasePolicyError, "receipt is absent"
        ):
            candidate._strict_external_receipts(
                self.policy(), release_source_sha=self.RELEASE_SOURCE_SHA
            )

    def test_external_receipt_expires_after_24_hours(self) -> None:
        now = datetime(2026, 8, 28, 10, 0, tzinfo=timezone.utc)
        policy = self.policy()
        trusted, watchdog = self.external_receipts(now - timedelta(hours=25), policy)
        with self.assertRaisesRegex(candidate.ReleasePolicyError, "stale"):
            candidate._strict_external_receipts(
                policy,
                release_source_sha=self.RELEASE_SOURCE_SHA,
                now=now,
                trusted_publisher_receipt=trusted,
                approval_watchdog_receipt=watchdog,
            )

    def test_fresh_matching_external_receipts_pass(self) -> None:
        now = datetime(2026, 8, 28, 10, 0, tzinfo=timezone.utc)
        policy = self.policy()
        trusted, watchdog = self.external_receipts(now - timedelta(minutes=5), policy)
        summaries = candidate._strict_external_receipts(
            policy,
            release_source_sha=self.RELEASE_SOURCE_SHA,
            now=now,
            trusted_publisher_receipt=trusted,
            approval_watchdog_receipt=watchdog,
        )
        self.assertEqual(
            set(summaries), {"trusted_publisher", "approval_watchdog"}
        )
        self.assertEqual(
            summaries["approval_watchdog"]["write_authority_sha256"],
            watchdog["write_authority_sha256"],
        )

    def test_watchdog_for_another_tag_is_rejected(self) -> None:
        now = datetime(2026, 8, 28, 10, 0, tzinfo=timezone.utc)
        policy = self.policy()
        trusted, watchdog = self.external_receipts(now, policy)
        watchdog["candidate_tag"] = "v0.4.2"
        with self.assertRaisesRegex(candidate.ReleasePolicyError, "coordinates"):
            candidate._strict_external_receipts(
                policy,
                release_source_sha=self.RELEASE_SOURCE_SHA,
                now=now,
                trusted_publisher_receipt=trusted,
                approval_watchdog_receipt=watchdog,
            )

    def test_watchdog_for_another_release_source_is_rejected(self) -> None:
        now = datetime(2026, 8, 28, 10, 0, tzinfo=timezone.utc)
        policy = self.policy()
        trusted, watchdog = self.external_receipts(now, policy)
        watchdog["target_head_sha"] = "b" * 40
        with self.assertRaisesRegex(candidate.ReleasePolicyError, "another release source"):
            candidate._strict_external_receipts(
                policy,
                release_source_sha=self.RELEASE_SOURCE_SHA,
                now=now,
                trusted_publisher_receipt=trusted,
                approval_watchdog_receipt=watchdog,
            )

    def test_watchdog_must_cover_the_full_remaining_approval_window(self) -> None:
        now = datetime(2026, 8, 28, 10, 0, tzinfo=timezone.utc)
        policy = self.policy()
        trusted, watchdog = self.external_receipts(now, policy)
        watchdog["active_until"] = (now + timedelta(hours=23, minutes=59)).isoformat()
        with self.assertRaisesRegex(candidate.ReleasePolicyError, "full approval window"):
            candidate._strict_external_receipts(
                policy,
                release_source_sha=self.RELEASE_SOURCE_SHA,
                now=now,
                trusted_publisher_receipt=trusted,
                approval_watchdog_receipt=watchdog,
            )

    def test_watchdog_receipt_requires_worker_version_identity(self) -> None:
        now = datetime(2026, 8, 28, 10, 0, tzinfo=timezone.utc)
        policy = self.policy()
        trusted, watchdog = self.external_receipts(now, policy)
        watchdog.pop("worker_version")
        with self.assertRaisesRegex(candidate.ReleasePolicyError, "Worker version identity"):
            candidate._strict_external_receipts(
                policy,
                release_source_sha=self.RELEASE_SOURCE_SHA,
                now=now,
                trusted_publisher_receipt=trusted,
                approval_watchdog_receipt=watchdog,
            )

    def test_watchdog_receipt_ref_binds_worker_version(self) -> None:
        now = datetime(2026, 8, 28, 10, 0, tzinfo=timezone.utc)
        policy = self.policy()
        trusted, watchdog = self.external_receipts(now, policy)
        watchdog["receipt_ref"] = (
            "cloudflare-worker://marvis-oss-release-watchdog-041/"
            f"another-worker/{'e' * 64}"
        )
        with self.assertRaisesRegex(candidate.ReleasePolicyError, "bind its Worker version"):
            candidate._strict_external_receipts(
                policy,
                release_source_sha=self.RELEASE_SOURCE_SHA,
                now=now,
                trusted_publisher_receipt=trusted,
                approval_watchdog_receipt=watchdog,
            )

    def test_watchdog_requires_persisted_write_authority_proof(self) -> None:
        now = datetime(2026, 8, 28, 10, 0, tzinfo=timezone.utc)
        policy = self.policy()
        trusted, watchdog = self.external_receipts(now, policy)
        watchdog.pop("write_authority")
        with self.assertRaisesRegex(candidate.ReleasePolicyError, "write-authority proof"):
            candidate._strict_external_receipts(
                policy,
                release_source_sha=self.RELEASE_SOURCE_SHA,
                now=now,
                trusted_publisher_receipt=trusted,
                approval_watchdog_receipt=watchdog,
            )

    def test_watchdog_write_authority_is_bound_to_the_release_source(self) -> None:
        now = datetime(2026, 8, 28, 10, 0, tzinfo=timezone.utc)
        policy = self.policy()
        trusted, watchdog = self.external_receipts(now, policy)
        watchdog["write_authority"]["target_head_sha"] = "b" * 40
        with self.assertRaisesRegex(candidate.ReleasePolicyError, "coordinates"):
            candidate._strict_external_receipts(
                policy,
                release_source_sha=self.RELEASE_SOURCE_SHA,
                now=now,
                trusted_publisher_receipt=trusted,
                approval_watchdog_receipt=watchdog,
            )

    def test_watchdog_write_authority_requires_distinct_canary_runs(self) -> None:
        now = datetime(2026, 8, 28, 10, 0, tzinfo=timezone.utc)
        policy = self.policy()
        trusted, watchdog = self.external_receipts(now, policy)
        cancel = watchdog["write_authority"]["capabilities"]["cancel_workflow_run"]
        cancel["run_id"] = 1001
        cancel["run_url"] = "https://github.com/emiliomartucci/marvis/actions/runs/1001"
        with self.assertRaisesRegex(candidate.ReleasePolicyError, "run identity"):
            candidate._strict_external_receipts(
                policy,
                release_source_sha=self.RELEASE_SOURCE_SHA,
                now=now,
                trusted_publisher_receipt=trusted,
                approval_watchdog_receipt=watchdog,
            )

    def test_watchdog_write_authority_binds_modes_to_one_nonce(self) -> None:
        now = datetime(2026, 8, 28, 10, 0, tzinfo=timezone.utc)
        policy = self.policy()
        trusted, watchdog = self.external_receipts(now, policy)
        reject = watchdog["write_authority"]["capabilities"][
            "reject_pending_deployment"
        ]
        reject["nonce"] = "watchdog-20260830-different"
        with self.assertRaisesRegex(candidate.ReleasePolicyError, "readback"):
            candidate._strict_external_receipts(
                policy,
                release_source_sha=self.RELEASE_SOURCE_SHA,
                now=now,
                trusted_publisher_receipt=trusted,
                approval_watchdog_receipt=watchdog,
            )

        trusted, watchdog = self.external_receipts(now, policy)
        watchdog["write_authority"]["capabilities"]["cancel_workflow_run"][
            "mode"
        ] = "reject-pending-deployment"
        with self.assertRaisesRegex(candidate.ReleasePolicyError, "readback"):
            candidate._strict_external_receipts(
                policy,
                release_source_sha=self.RELEASE_SOURCE_SHA,
                now=now,
                trusted_publisher_receipt=trusted,
                approval_watchdog_receipt=watchdog,
            )

    def test_watchdog_write_authority_requires_worker_attestation(self) -> None:
        now = datetime(2026, 8, 28, 10, 0, tzinfo=timezone.utc)
        policy = self.policy()
        trusted, watchdog = self.external_receipts(now, policy)
        watchdog["write_authority"]["worker_attestation"]["signature"] = "bad"
        with self.assertRaisesRegex(candidate.ReleasePolicyError, "attestation"):
            candidate._strict_external_receipts(
                policy,
                release_source_sha=self.RELEASE_SOURCE_SHA,
                now=now,
                trusted_publisher_receipt=trusted,
                approval_watchdog_receipt=watchdog,
            )

    def test_watchdog_write_authority_proof_expires_after_24_hours(self) -> None:
        now = datetime(2026, 8, 28, 10, 0, tzinfo=timezone.utc)
        policy = self.policy()
        trusted, watchdog = self.external_receipts(now, policy)
        watchdog["write_authority"]["verified_at"] = (
            now - timedelta(hours=25)
        ).isoformat()
        with self.assertRaisesRegex(candidate.ReleasePolicyError, "stale or unordered"):
            candidate._strict_external_receipts(
                policy,
                release_source_sha=self.RELEASE_SOURCE_SHA,
                now=now,
                trusted_publisher_receipt=trusted,
                approval_watchdog_receipt=watchdog,
            )

    def test_watchdog_write_authority_digest_is_immutable(self) -> None:
        now = datetime(2026, 8, 28, 10, 0, tzinfo=timezone.utc)
        policy = self.policy()
        trusted, watchdog = self.external_receipts(now, policy)
        watchdog["write_authority_sha256"] = "0" * 64
        with self.assertRaisesRegex(candidate.ReleasePolicyError, "digest"):
            candidate._strict_external_receipts(
                policy,
                release_source_sha=self.RELEASE_SOURCE_SHA,
                now=now,
                trusted_publisher_receipt=trusted,
                approval_watchdog_receipt=watchdog,
            )

    def test_write_authority_canary_workflow_is_bounded_and_non_publishing(self) -> None:
        workflow_path = ROOT / ".github/workflows/watchdog-canary.yml"
        workflow = yaml.load(workflow_path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
        self.assertEqual(set(workflow["on"]), {"workflow_dispatch"})
        self.assertEqual(workflow["permissions"], {"contents": "read"})
        self.assertEqual(
            set(workflow["jobs"]),
            {"reject-pending-deployment", "cancel-workflow-run"},
        )
        self.assertEqual(
            workflow["jobs"]["reject-pending-deployment"]["environment"], "pypi"
        )
        self.assertEqual(
            workflow["jobs"]["reject-pending-deployment"]["timeout-minutes"], "1"
        )
        self.assertEqual(
            workflow["jobs"]["cancel-workflow-run"]["timeout-minutes"], "2"
        )
        source = workflow_path.read_text(encoding="utf-8")
        self.assertNotIn("uses:", source)
        self.assertNotIn("publish", source.lower())

    def test_manifest_byte_tamper_is_rejected(self) -> None:
        with self.active_candidate_for_unit_test(), tempfile.TemporaryDirectory(
            prefix="release-manifest-"
        ) as raw:
            dist = Path(raw) / "dist"
            artifact, _ = self.write_release_artifacts(dist)
            manifest = candidate.build_manifest(ROOT, dist)
            manifest_path = Path(raw) / "manifest.json"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            artifact.write_bytes(b"tampered")
            with self.assertRaisesRegex(candidate.ReleasePolicyError, "bytes differ"):
                candidate.verify_manifest(ROOT, manifest_path, dist)

    def test_manifest_hashes_only_the_post_foundation_release_delta(self) -> None:
        policy = self.policy()
        shared_source = candidate._shared_source_coordinates(ROOT, policy)
        state = candidate._candidate_state(policy, shared_source=shared_source)
        with tempfile.TemporaryDirectory(prefix="release-foundation-manifest-") as raw:
            dist = Path(raw) / "dist"
            self.write_release_artifacts(dist)
            if state["status"] == "invalidated":
                with self.assertRaisesRegex(candidate.ReleasePolicyError, "invalidated"):
                    candidate.build_manifest(ROOT, dist)
                return
            manifest = candidate.build_manifest(ROOT, dist)
        expected_delta = candidate._git(
            ROOT,
            "diff",
            "--binary",
            "--full-index",
            f"{policy['release_foundation']['merge_sha']}..HEAD",
            text=False,
        )
        self.assertEqual(
            manifest["allowed_release_delta_sha256"],
            candidate._sha_bytes(expected_delta),
        )
        self.assertNotIn(".github/workflows/ci.yml", manifest["changed_paths"])

    def test_manifest_recomputed_identity_tamper_is_rejected(self) -> None:
        with self.active_candidate_for_unit_test(), tempfile.TemporaryDirectory(
            prefix="release-identity-"
        ) as raw:
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
        with self.active_candidate_for_unit_test(), tempfile.TemporaryDirectory(
            prefix="release-assets-"
        ) as raw:
            dist = Path(raw) / "dist"
            self.write_release_artifacts(dist)
            manifest_path = dist / "release-manifest.json"
            manifest = candidate.build_manifest(ROOT, dist)
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            (dist / "unreviewed-installer.sh").write_text("exit 0\n", encoding="utf-8")
            with self.assertRaisesRegex(candidate.ReleasePolicyError, "file set"):
                candidate.verify_manifest(ROOT, manifest_path, dist)

    def test_acceptance_receipt_binds_registry_and_workflow(self) -> None:
        now = datetime(2026, 8, 28, 10, 0, tzinfo=timezone.utc)
        policy = self.verified_external_policy(now)
        with self.active_candidate_for_unit_test(), tempfile.TemporaryDirectory(
            prefix="release-acceptance-"
        ) as raw:
            root = Path(raw)
            registry_dist = root / "registry"
            github_dist = root / "github"
            self.write_release_artifacts(registry_dist)
            self.write_release_artifacts(github_dist)
            dispatch = {
                "id": 77,
                "run_attempt": 1,
                "head_sha": str(candidate._git(ROOT, "rev-parse", "HEAD")),
                "event": "workflow_dispatch",
                "conclusion": "success",
            }
            manifest = candidate.build_manifest(
                ROOT,
                registry_dist,
                dispatch_preflight=dispatch,
            )
            manifest_path = root / "release-manifest.json"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            (github_dist / manifest_path.name).write_text(
                json.dumps(manifest), encoding="utf-8"
            )
            run = {
                "head_sha": manifest["release_source_sha"],
                "path": str(candidate.WORKFLOW_PATH),
                "event": "push",
                "head_branch": policy["candidate_tag"],
                "head_repository": {"full_name": policy["repository"]},
            }
            with mock.patch.object(
                candidate, "load_policy", return_value=policy
            ), mock.patch.object(
                candidate,
                "_strict_external_receipts",
                return_value={"trusted_publisher": {}, "approval_watchdog": {}},
            ), mock.patch.object(
                candidate,
                "_shared_source_readback",
                return_value=manifest["shared_source"],
            ), mock.patch.object(
                candidate,
                "_release_foundation_readback",
                return_value=manifest["release_foundation"],
            ), mock.patch.object(
                candidate, "verify_manifest"
            ), mock.patch.object(
                candidate,
                "registry_verify",
                return_value={"status": "registry_verified", "files": 2},
            ), mock.patch.object(
                candidate, "_remote_tag_sha", return_value=manifest["release_source_sha"]
            ), mock.patch.object(
                candidate,
                "_require_remote_release_source",
                return_value=manifest["release_source_sha"],
            ), mock.patch.object(
                candidate,
                "_draft_release",
                return_value={"id": 1, "draft": True, "prerelease": True},
            ), mock.patch.object(
                candidate, "_workflow_run_readback", return_value=run
            ), mock.patch.object(
                candidate, "_workflow_dispatch_proof", return_value=dispatch
            ):
                receipt = candidate.build_acceptance_receipt(
                    ROOT,
                    manifest_path,
                    registry_dist,
                    github_dist,
                    token="masked",
                    run_id="123",
                    now=now,
                    api_url="https://api.example",
                )
            claimed = receipt.pop("content_digest")
            self.assertEqual(claimed, candidate._sha_bytes(candidate._canonical(receipt)))
            self.assertEqual(
                receipt["terminal_state"],
                "accepted_ready_for_github_release_finalization",
            )
            self.assertEqual(
                receipt["release_foundation"]["merge_sha"],
                policy["release_foundation"]["merge_sha"],
            )

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
            response.read.side_effect = [raw_artifact[:3], raw_artifact[3:], b""]
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

    def test_registry_download_failure_removes_partial_and_final_files(self) -> None:
        expected = b"payload"
        sha256 = candidate._sha_bytes(expected)
        url = "https://files.pythonhosted.org/packages/a/a.whl"
        cases = {
            "oversized": [expected + b"x"],
            "short": [expected[:-1], b""],
            "checksum": [b"PAYLOAD", b""],
            "midstream": [expected[:3], OSError("connection reset")],
        }
        for label, chunks in cases.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory(
                prefix=f"registry-{label}-"
            ) as raw:
                root = Path(raw)
                manifest_path = root / "manifest.json"
                self.write_manifest(
                    manifest_path,
                    package="marvisx-cli",
                    version="0.4.1",
                    artifacts=[
                        {
                            "filename": "a.whl",
                            "size": len(expected),
                            "sha256": sha256,
                        }
                    ],
                )
                payload = {
                    "info": {"name": "marvisx-cli", "version": "0.4.1"},
                    "urls": [
                        {
                            "filename": "a.whl",
                            "size": len(expected),
                            "digests": {"sha256": sha256},
                            "yanked": False,
                            "url": url,
                        }
                    ],
                }
                response = mock.MagicMock()
                response.__enter__.return_value = response
                response.read.side_effect = chunks
                response.geturl.return_value = url
                destination = root / "download"
                with mock.patch.object(
                    candidate, "_request_json", return_value=payload
                ), mock.patch.object(
                    candidate.urllib.request, "urlopen", return_value=response
                ), self.assertRaises((candidate.ReleasePolicyError, OSError)):
                    candidate.registry_download(
                        manifest_path,
                        destination,
                        attempts=1,
                        delay_seconds=0,
                    )
                self.assertEqual(list(destination.iterdir()), [])

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

    def test_installed_upgrade_wrapper_excludes_checkout_product_imports(self) -> None:
        with tempfile.TemporaryDirectory(prefix="installed-candidate-") as raw:
            installed = Path(raw) / "site-packages"
            (installed / "migrations").mkdir(parents=True)
            (installed / "core/api").mkdir(parents=True)
            (installed / "core/api/__init__.py").write_text(
                "ORIGIN = 'installed'\n", encoding="utf-8"
            )

            prior = types.SimpleNamespace(version="0.3.8")
            verifier = types.SimpleNamespace()
            verifier.UpgradeVerificationError = RuntimeError
            verifier._load_contract = lambda _path: [prior]
            verifier._download = lambda _prior, directory: directory / "prior.whl"

            def verify_upgrade(root, _artifact, _prior, *, evidence_dir):
                import core.api

                self.assertEqual(core.api.ORIGIN, "installed")
                self.assertEqual(root, installed.resolve())
                self.assertIsNotNone(evidence_dir)
                return {"rollback_status": "rolled_back"}

            verifier.verify_upgrade = verify_upgrade
            loader = types.SimpleNamespace(exec_module=lambda _module: None)
            spec = types.SimpleNamespace(name="_test_upgrade_verifier", loader=loader)

            class Distribution:
                version = "0.4.1"

                @staticmethod
                def locate_file(_value):
                    return installed

            for name in [
                module_name
                for module_name in list(candidate.sys.modules)
                if module_name == "core" or module_name.startswith("core.")
            ]:
                candidate.sys.modules.pop(name, None)
            with mock.patch.object(
                candidate.importlib.metadata,
                "distribution",
                return_value=Distribution(),
            ), mock.patch.object(
                candidate.importlib.util,
                "spec_from_file_location",
                return_value=spec,
            ), mock.patch.object(
                candidate.importlib.util,
                "module_from_spec",
                return_value=verifier,
            ):
                report = candidate.verify_installed_upgrade(
                    ROOT,
                    version="0.3.8",
                    evidence_dir=Path(raw) / "evidence",
                )
            self.assertEqual(report["candidate_import_origin"], "installed_distribution")
            self.assertEqual(report["candidate_distribution_version"], "0.4.1")

    def test_release_entrypoint_does_not_import_build_only_packaging_eagerly(self) -> None:
        with tempfile.TemporaryDirectory(prefix="blocked-packaging-") as raw:
            blocked = Path(raw) / "packaging"
            blocked.mkdir()
            (blocked / "__init__.py").write_text(
                "raise AssertionError('packaging imported eagerly')\n",
                encoding="utf-8",
            )
            environment = os.environ.copy()
            environment["PYTHONPATH"] = str(Path(raw))
            result = subprocess.run(
                [
                    candidate.sys.executable,
                    str(ROOT / "scripts/release_candidate.py"),
                    "--help",
                ],
                cwd=ROOT,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("packaging imported eagerly", result.stderr)

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
            candidate,
            "_shared_source_readback",
            return_value=candidate._shared_source_coordinates(ROOT, policy),
        ), mock.patch.object(
            candidate,
            "_release_foundation_readback",
            return_value=candidate._release_foundation_coordinates(ROOT, policy),
        ), mock.patch.object(
            candidate, "_require_release_controls_committed"
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
            candidate,
            "_shared_source_readback",
            return_value=candidate._shared_source_coordinates(ROOT, policy),
        ), mock.patch.object(
            candidate,
            "_release_foundation_readback",
            return_value=candidate._release_foundation_coordinates(ROOT, policy),
        ), mock.patch.object(
            candidate,
            "_workflow_dispatch_proof",
            return_value={"id": 1, "run_attempt": 1, "head_sha": reviewed},
        ), mock.patch.object(
            candidate, "_require_release_controls_committed"
        ), mock.patch.object(
            candidate, "_environment_check", return_value={"name": "pypi"}
        ), mock.patch.object(
            candidate, "_remote_tag_sha", return_value=reviewed
        ), mock.patch.object(
            candidate, "_require_remote_release_source", return_value=reviewed
        ), mock.patch.object(
            candidate, "_registry_history_check", return_value={"info": {"version": "0.3.8"}}
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
            candidate,
            "_shared_source_readback",
            return_value=candidate._shared_source_coordinates(ROOT, policy),
        ), mock.patch.object(
            candidate,
            "_release_foundation_readback",
            return_value=candidate._release_foundation_coordinates(ROOT, policy),
        ), mock.patch.object(
            candidate, "_require_release_controls_committed"
        ), mock.patch.object(
            candidate, "_environment_check", return_value={"name": "pypi"}
        ), mock.patch.object(
            candidate,
            "_require_remote_release_source",
            side_effect=candidate.ReleasePolicyError(
                "release source is not the exact remote release-branch head"
            ),
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
            candidate,
            "_shared_source_readback",
            return_value=candidate._shared_source_coordinates(ROOT, policy),
        ), mock.patch.object(
            candidate,
            "_release_foundation_readback",
            return_value=candidate._release_foundation_coordinates(ROOT, policy),
        ), mock.patch.object(
            candidate, "_require_release_controls_committed"
        ), mock.patch.object(
            candidate, "_environment_check", return_value={"name": "pypi"}
        ), mock.patch.object(
            candidate, "_require_remote_release_source", return_value=reviewed
        ), mock.patch.object(
            candidate, "_registry_history_check", return_value={"info": {"version": "0.3.8"}}
        ), mock.patch.object(
            candidate,
            "_request_json",
            side_effect=[
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
            "deployment_branch_policy": None,
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

    def test_environment_extra_reviewer_is_rejected(self) -> None:
        policy = self.policy()
        response = {
            "name": "pypi",
            "can_admins_bypass": False,
            "deployment_branch_policy": None,
            "protection_rules": [
                {
                    "type": "required_reviewers",
                    "prevent_self_review": False,
                    "reviewers": [
                        {"reviewer": {"login": "emiliomartucci"}},
                        {"reviewer": {"login": "another-user"}},
                    ],
                }
            ],
        }
        with mock.patch.object(candidate, "_request_json", return_value=response):
            with self.assertRaisesRegex(candidate.ReleasePolicyError, "exactly"):
                candidate._environment_check(
                    policy, token="masked", api_url="https://api.example"
                )

    def test_tag_preflight_rejects_unmerged_release_source(self) -> None:
        policy = self.policy()
        reviewed = "a" * 40
        with mock.patch.object(
            candidate, "validate_static", return_value={"release_source_sha": reviewed}
        ), mock.patch.object(candidate, "load_policy", return_value=policy), mock.patch.object(
            candidate, "_strict_external_receipts"
        ), mock.patch.object(
            candidate,
            "_shared_source_readback",
            return_value=candidate._shared_source_coordinates(ROOT, policy),
        ), mock.patch.object(
            candidate,
            "_release_foundation_readback",
            return_value=candidate._release_foundation_coordinates(ROOT, policy),
        ), mock.patch.object(
            candidate, "_require_release_controls_committed"
        ), mock.patch.object(
            candidate, "_environment_check", return_value={"name": "pypi"}
        ), mock.patch.object(
            candidate, "_remote_tag_sha", return_value=reviewed
        ), mock.patch.object(
            candidate,
            "_require_remote_release_source",
            side_effect=candidate.ReleasePolicyError(
                "release source is not the exact remote release-branch head"
            ),
        ):
            with self.assertRaisesRegex(candidate.ReleasePolicyError, "exact remote"):
                candidate.tag_preflight(
                    ROOT, token="masked", api_url="https://api.example"
                )

    def test_publication_window_rejects_late_approval(self) -> None:
        now = datetime(2026, 8, 28, 10, 0, tzinfo=timezone.utc)
        policy = self.verified_external_policy(now)
        with tempfile.TemporaryDirectory(prefix="approval-window-") as raw:
            manifest_path = Path(raw) / "manifest.json"
            self.write_manifest(
                manifest_path,
                release_source_sha="a" * 40,
                release_foundation=candidate._release_foundation_coordinates(
                    ROOT, policy
                ),
                shared_source=candidate._shared_source_coordinates(ROOT, policy),
                dispatch_preflight={"id": 7, "run_attempt": 1, "head_sha": "a" * 40},
            )
            run = {
                "created_at": (now - timedelta(hours=25)).isoformat(),
                "head_sha": "a" * 40,
                "path": str(candidate.WORKFLOW_PATH),
                "event": "push",
                "head_branch": policy["candidate_tag"],
                "head_repository": {"full_name": policy["repository"]},
            }
            with mock.patch.object(
                candidate, "load_policy", return_value=policy
            ), mock.patch.object(
                candidate, "_strict_external_receipts", return_value={}
            ), mock.patch.object(
                candidate,
                "_shared_source_readback",
                return_value=candidate._shared_source_coordinates(ROOT, policy),
            ), mock.patch.object(
                candidate,
                "_release_foundation_readback",
                return_value=candidate._release_foundation_coordinates(ROOT, policy),
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
            self.write_manifest(
                manifest_path,
                release_source_sha="a" * 40,
                release_foundation=candidate._release_foundation_coordinates(
                    ROOT, policy
                ),
                shared_source=candidate._shared_source_coordinates(ROOT, policy),
                dispatch_preflight={"id": 7, "run_attempt": 1, "head_sha": "a" * 40},
            )
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
                candidate, "_strict_external_receipts", return_value={}
            ), mock.patch.object(
                candidate,
                "_shared_source_readback",
                return_value=candidate._shared_source_coordinates(ROOT, policy),
            ), mock.patch.object(
                candidate,
                "_release_foundation_readback",
                return_value=candidate._release_foundation_coordinates(ROOT, policy),
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
            self.write_manifest(
                manifest_path,
                release_source_sha="a" * 40,
                release_foundation=candidate._release_foundation_coordinates(
                    ROOT, policy
                ),
                shared_source=candidate._shared_source_coordinates(ROOT, policy),
                dispatch_preflight={"id": 7, "run_attempt": 1, "head_sha": "a" * 40},
            )
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
                candidate, "_strict_external_receipts", return_value={}
            ), mock.patch.object(
                candidate,
                "_shared_source_readback",
                return_value=candidate._shared_source_coordinates(ROOT, policy),
            ), mock.patch.object(
                candidate,
                "_release_foundation_readback",
                return_value=candidate._release_foundation_coordinates(ROOT, policy),
            ), mock.patch.object(
                candidate,
                "_workflow_dispatch_proof",
                return_value={"id": 7, "run_attempt": 1, "head_sha": "a" * 40},
            ), mock.patch.object(
                candidate, "_request_json", return_value=run
            ), mock.patch.object(
                candidate, "_remote_tag_sha", return_value="a" * 40
            ), mock.patch.object(
                candidate, "_require_remote_release_source", return_value="a" * 40
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
