#!/usr/bin/env python3
"""Tests for scripts/import_shared_projection.py — green on a good bundle,
red on every tampered, forbidden, or ownership-violating one (plan U2 test
scenarios: bad digest, wrong profile, forbidden path, symlink escape, secret
sentinel, undeclared overlap, missing shared path, deterministic repeat run).
"""

from __future__ import annotations

import contextlib
import io
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import import_shared_projection as isp  # noqa: E402

SOURCE_SHA = "a" * 40
EXPORTER_SHA = "b" * 40
EXPORTER_IDENTITY = "c" * 64

TEST_MAP = """\
schema: marvis-shared-ownership/v1
ownership_map_version: 1
managed_areas:
  - core/api/
  - migrations/
  - CHANGELOG.md
oss_owned_areas:
  - README.md
  - pyproject.toml
  - core/api/tests/
approved_preserve_paths: []
deny:
  forbidden_prefixes:
    - core/hosted_control/
    - cloud-operations/
  forbidden_components:
    - .env
    - secrets
  forbidden_suffixes:
    - .pem
    - .db
  forbidden_content_markers:
    - MARVIS_PRIVATE_SENTINEL_DO_NOT_EXPORT
  secret_patterns:
    - "AKIA[0-9A-Z]{16}"
  forbidden_imports:
    - core.hosted_lifecycle
    - workos
policy:
  never_delete: true
  apply_requires_clean_worktree: true
  max_file_bytes: 1048576
"""

ENGINE_PIN = """\
engine: marvisx
contract_version: 1
engine_ref: %s
openapi_baseline: marvisx:contracts/openapi/marvisx.json
compatibility_window: N/N-1
""" % ("d" * 40)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def build_repo(base: Path) -> Path:
    repo = base / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    files = {
        "README.md": "oss readme\n",
        "pyproject.toml": '[project]\nname = "test"\n',
        "contracts/engine-pin.yaml": ENGINE_PIN,
        "contracts/shared-ownership.yaml": TEST_MAP,
        "core/api/engine.py": "ENGINE = 1\n",
        "core/api/local_only.py": "LOCAL = True\n",
        "core/api/tests/test_engine.py": "def test():\n    assert True\n",
        "migrations/001_init.py": "# init\n",
        "migrations/002_next.py": "# next\n",
        "CHANGELOG.md": "# changelog\n",
        "docs/decisions/adr.md": "# adr\n",
    }
    for path, content in files.items():
        (repo / path).parent.mkdir(parents=True, exist_ok=True)
        (repo / path).write_text(content, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    return repo


def build_bundle(base: Path, files: dict[str, tuple[bytes, str]]) -> Path:
    bundle = base / "bundle"
    payload_root = bundle / "payload"
    records = []
    manifest_files = []
    for path in sorted(files):
        content, mode = files[path]
        target = payload_root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        manifest_files.append(
            {
                "git_oid": isp.git_blob_oid(content),
                "mode": mode,
                "output_path": path,
                "sha256": isp.sha256_bytes(content),
                "size": len(content),
                "source_path": "src/" + path,
            }
        )
        records.append(
            {"mode": mode, "path": path, "sha256": isp.sha256_bytes(content), "size": len(content)}
        )
    import_paths = sorted(files)
    payload_manifest = {
        "schema": "marvis-public-shared-payload/v1",
        "source_sha": SOURCE_SHA,
        "exporter_sha": EXPORTER_SHA,
        "exporter_identity_sha256": EXPORTER_IDENTITY,
        "payload_sha256": isp.payload_digest(records),
        "file_count": len(records),
        "files": manifest_files,
    }
    oss_manifest = {
        "schema": "marvis-projection-candidate/v1",
        "consumer": "oss",
        "payload_sha256": payload_manifest["payload_sha256"],
        "payload_file_count": len(records),
        "import_file_count": len(records),
        "preserved_overlap_paths": [],
        "import_paths": import_paths,
        "source_sha": SOURCE_SHA,
        "exporter_sha": EXPORTER_SHA,
        "exporter_identity_sha256": EXPORTER_IDENTITY,
    }
    manifests = bundle / "manifests"
    manifests.mkdir(parents=True, exist_ok=True)
    (manifests / "payload.json").write_bytes(isp.canonical_json(payload_manifest))
    (manifests / "oss.json").write_bytes(isp.canonical_json(oss_manifest))
    return bundle


def green_files() -> dict[str, tuple[bytes, str]]:
    return {
        "core/api/engine.py": (b"ENGINE = 2\n", "100644"),
        "core/api/new_module.py": (b"NEW = True\n", "100644"),
        "migrations/001_init.py": (b"# init\n", "100644"),
        "migrations/002_next.py": (b"# next\n", "100644"),
        "migrations/003_added.py": (b"# added\n", "100644"),
        "CHANGELOG.md": (b"# changelog\n", "100644"),
    }


def inject_entry(bundle: Path, path: str, mode: str = "100644") -> None:
    """Add a manifest-only entry for a path that can never exist on disk.

    Unsafe paths (absolute, traversal, control characters) are refused at
    manifest validation, before the importer ever looks for the file — this
    is exactly the layer under test.
    """
    manifest_path = bundle / "manifests/payload.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"].append(
        {
            "git_oid": "0" * 40,
            "mode": mode,
            "output_path": path,
            "sha256": "0" * 64,
            "size": 1,
            "source_path": "src/" + path,
        }
    )
    manifest["file_count"] = len(manifest["files"])
    manifest_path.write_bytes(isp.canonical_json(manifest))


def make_args(repo: Path, bundle: Path, **overrides):
    payload_manifest = json.loads((bundle / "manifests/payload.json").read_text(encoding="utf-8"))
    consumer_manifest_bytes = (bundle / "manifests/oss.json").read_bytes()
    defaults = {
        "repo": repo,
        "bundle": bundle,
        "ownership_map": repo / "contracts/shared-ownership.yaml",
        "mode": "dry-run",
        "expected_source_sha": SOURCE_SHA,
        "expected_exporter_sha": EXPORTER_SHA,
        "expected_exporter_identity_sha256": EXPORTER_IDENTITY,
        "expected_payload_sha256": payload_manifest["payload_sha256"],
        "expected_consumer_manifest_sha256": isp.sha256_bytes(consumer_manifest_bytes),
        "backup_dir": None,
        "report": None,
    }
    defaults.update(overrides)
    return type("Args", (), defaults)


def bundle_expectations(bundle: Path) -> dict[str, str]:
    args = make_args(Path("."), bundle)
    return {
        "source_sha": args.expected_source_sha,
        "exporter_sha": args.expected_exporter_sha,
        "exporter_identity_sha256": args.expected_exporter_identity_sha256,
        "payload_sha256": args.expected_payload_sha256,
        "consumer_manifest_sha256": args.expected_consumer_manifest_sha256,
    }


class ImportGateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.base = Path(tempfile.mkdtemp(prefix="import-gate-"))
        self.repo = build_repo(self.base)
        self.addCleanup(shutil.rmtree, self.base, ignore_errors=True)

    def refused(self, args) -> str:
        """run() fails closed by returning 2 and printing the refusal."""
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            exit_code = isp.run(args)
        self.assertEqual(exit_code, 2, f"expected refusal, got {exit_code}")
        return stderr.getvalue()

    # -- green paths -------------------------------------------------------

    def test_green_bundle_verifies_and_is_deterministic(self) -> None:
        bundle = build_bundle(self.base, green_files())
        first = self.base / "report1.json"
        second = self.base / "report2.json"
        result_one = isp.run(make_args(self.repo, bundle, report=first))
        result_two = isp.run(make_args(self.repo, bundle, report=second))
        self.assertEqual((result_one, result_two), (0, 0))
        self.assertEqual(first.read_bytes(), second.read_bytes())
        report = json.loads(first.read_text(encoding="utf-8"))
        self.assertEqual(report["status"], "verified")
        self.assertEqual(report["payload"]["file_count"], 6)
        self.assertEqual(report["payload"]["importable_file_count"], 6)
        self.assertEqual(report["classification"]["already_synced"], ["CHANGELOG.md", "migrations/001_init.py", "migrations/002_next.py"])
        self.assertEqual(report["classification"]["additions"], ["core/api/new_module.py", "migrations/003_added.py"])
        self.assertEqual(report["classification"]["would_overwrite"], ["core/api/engine.py"])
        self.assertIn("core/api/local_only.py", report["classification"]["local_only_in_managed_area"])
        self.assertEqual(report["compatibility"]["migrations"]["changed_in_payload"], 0)
        self.assertTrue(report["digests"]["proposed_tree_sha256"])

    def test_cli_smoke_green_bundle(self) -> None:
        bundle = build_bundle(self.base, green_files())
        expected = bundle_expectations(bundle)
        process = subprocess.run(
            [
                sys.executable,
                str(Path(__file__).parent / "import_shared_projection.py"),
                "--repo", str(self.repo),
                "--bundle", str(bundle),
                "--expected-source-sha", SOURCE_SHA,
                "--expected-exporter-sha", EXPORTER_SHA,
                "--expected-exporter-identity-sha256", EXPORTER_IDENTITY,
                "--expected-payload-sha256", expected["payload_sha256"],
                "--expected-consumer-manifest-sha256", expected["consumer_manifest_sha256"],
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertIn("status=verified", process.stdout)

    # -- identity gates ----------------------------------------------------

    def test_expected_source_sha_mismatch_refused(self) -> None:
        bundle = build_bundle(self.base, green_files())
        self.refused(make_args(self.repo, bundle, expected_source_sha="e" * 40))

    def test_tampered_payload_bytes_refused(self) -> None:
        bundle = build_bundle(self.base, green_files())
        (bundle / "payload/core/api/engine.py").write_bytes(b"ENGINE = 99\n")
        self.refused(make_args(self.repo, bundle))

    def test_tampered_manifest_digest_refused(self) -> None:
        bundle = build_bundle(self.base, green_files())
        manifest_path = bundle / "manifests/payload.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["payload_sha256"] = "0" * 64
        manifest_path.write_bytes(isp.canonical_json(manifest))
        self.refused(make_args(self.repo, bundle))

    def test_consumer_manifest_binding_refused(self) -> None:
        bundle = build_bundle(self.base, green_files())
        manifest_path = bundle / "manifests/oss.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["payload_sha256"] = "0" * 64
        manifest_path.write_bytes(isp.canonical_json(manifest))
        self.refused(make_args(self.repo, bundle))

    def test_wrong_consumer_profile_refused(self) -> None:
        bundle = build_bundle(self.base, green_files())
        manifest_path = bundle / "manifests/oss.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["consumer"] = "enterprise"
        manifest_path.write_bytes(isp.canonical_json(manifest))
        self.refused(make_args(self.repo, bundle))

    def test_missing_payload_file_refused(self) -> None:
        bundle = build_bundle(self.base, green_files())
        (bundle / "payload/core/api/new_module.py").unlink()
        self.refused(make_args(self.repo, bundle))

    def test_unlisted_extra_payload_file_refused(self) -> None:
        bundle = build_bundle(self.base, green_files())
        (bundle / "payload/core/api/smuggled.py").write_bytes(b"x = 1\n")
        self.refused(make_args(self.repo, bundle))

    def test_every_expected_identity_is_required(self) -> None:
        bundle = build_bundle(self.base, green_files())
        for attribute in (
            "expected_source_sha",
            "expected_exporter_sha",
            "expected_exporter_identity_sha256",
            "expected_payload_sha256",
            "expected_consumer_manifest_sha256",
        ):
            with self.subTest(attribute=attribute):
                args = make_args(self.repo, bundle, **{attribute: None})
                self.assertEqual(isp.run(args), 2)

    def test_wrong_expected_consumer_manifest_digest_refused(self) -> None:
        bundle = build_bundle(self.base, green_files())
        self.refused(
            make_args(
                self.repo,
                bundle,
                expected_consumer_manifest_sha256="f" * 64,
            )
        )

    def test_consumer_identity_mismatch_refused(self) -> None:
        for field, value in (
            ("source_sha", "e" * 40),
            ("exporter_sha", "e" * 40),
            ("exporter_identity_sha256", "e" * 64),
        ):
            with self.subTest(field=field):
                bundle = build_bundle(self.base, green_files())
                manifest_path = bundle / "manifests/oss.json"
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                manifest[field] = value
                manifest_path.write_bytes(isp.canonical_json(manifest))
                self.refused(make_args(self.repo, bundle))

    def test_consumer_counts_and_coverage_drift_refused(self) -> None:
        mutations = (
            ("payload_file_count", lambda manifest: manifest.__setitem__("payload_file_count", 999)),
            ("import_file_count", lambda manifest: manifest.__setitem__("import_file_count", 999)),
            (
                "omitted_path",
                lambda manifest: (
                    manifest["import_paths"].pop(),
                    manifest.__setitem__("import_file_count", len(manifest["import_paths"])),
                ),
            ),
            (
                "duplicate_path",
                lambda manifest: (
                    manifest["import_paths"].append(manifest["import_paths"][0]),
                    manifest.__setitem__("import_file_count", len(manifest["import_paths"])),
                ),
            ),
            (
                "overlap",
                lambda manifest: manifest["preserved_overlap_paths"].append(manifest["import_paths"][0]),
            ),
        )
        for name, mutate in mutations:
            with self.subTest(name=name):
                bundle = build_bundle(self.base, green_files())
                manifest_path = bundle / "manifests/oss.json"
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                mutate(manifest)
                manifest_path.write_bytes(isp.canonical_json(manifest))
                self.refused(make_args(self.repo, bundle))

    # -- unsafe paths ------------------------------------------------------

    def test_path_traversal_refused(self) -> None:
        for unsafe in ("../evil.py", "core/api/../../evil.py"):
            with self.subTest(unsafe=unsafe):
                bundle = build_bundle(self.base, green_files())
                inject_entry(bundle, unsafe)
                self.refused(make_args(self.repo, bundle))

    def test_absolute_and_backslash_paths_refused(self) -> None:
        for unsafe in ("/etc/passwd", "core\\api\\x.py"):
            with self.subTest(unsafe=unsafe):
                bundle = build_bundle(self.base, green_files())
                inject_entry(bundle, unsafe)
                self.refused(make_args(self.repo, bundle))

    def test_non_nfc_and_control_character_paths_refused(self) -> None:
        decomposed = "core/api/cafe" + "́" + ".py"
        control = "core/api/ev" + chr(13) + "il.py"
        for unsafe in (decomposed, control):
            with self.subTest(unsafe=repr(unsafe)):
                bundle = build_bundle(self.base, green_files())
                inject_entry(bundle, unsafe)
                self.refused(make_args(self.repo, bundle))

    def test_duplicate_output_path_refused(self) -> None:
        bundle = build_bundle(self.base, {"core/api/engine.py": (b"ENGINE = 2\n", "100644")})
        duplicate = json.loads((bundle / "manifests/payload.json").read_text(encoding="utf-8"))["files"][0]
        inject_entry(bundle, duplicate["output_path"])
        self.refused(make_args(self.repo, bundle))

    def test_case_collision_inside_manifest_refused(self) -> None:
        files = {
            "core/api/engine.py": (b"ENGINE = 2\n", "100644"),
            "core/api/ENGINE.py": (b"SHADOW = 1\n", "100644"),
        }
        bundle = build_bundle(self.base, files)
        self.refused(make_args(self.repo, bundle))

    def test_case_collision_against_tracked_file_reported(self) -> None:
        bundle = build_bundle(self.base, {"core/api/LOCAL_ONLY.py": (b"SHADOW = 1\n", "100644")})
        exit_code = isp.run(make_args(self.repo, bundle))
        self.assertEqual(exit_code, 2)
        # Visible in the violations of a forced classification run.
        state = isp.load_bundle(bundle, bundle_expectations(bundle))
        ownership, _ = isp.load_ownership_map(self.repo / "contracts/shared-ownership.yaml")
        result = isp.classify(state, ownership, self.repo)
        self.assertTrue(any("case-insensitive collision" in v for v in result["violations"]))

    def test_symlink_payload_file_refused(self) -> None:
        bundle = build_bundle(self.base, green_files())
        target = bundle / "payload/core/api/new_module.py"
        target.unlink()
        target.symlink_to("/etc/passwd")
        self.refused(make_args(self.repo, bundle))

    def test_invalid_mode_refused(self) -> None:
        bundle = build_bundle(self.base, green_files())
        inject_entry(bundle, "core/api/badmode.py", mode="100666")
        self.refused(make_args(self.repo, bundle))

    # -- deny rules --------------------------------------------------------

    def forbidden(self, files: dict[str, tuple[bytes, str]], needle: str) -> None:
        bundle = build_bundle(self.base, files)
        exit_code = isp.run(make_args(self.repo, bundle))
        self.assertEqual(exit_code, 2)
        state = isp.load_bundle(bundle, bundle_expectations(bundle))
        ownership, _ = isp.load_ownership_map(self.repo / "contracts/shared-ownership.yaml")
        result = isp.classify(state, ownership, self.repo)
        self.assertTrue(any(needle in violation for violation in result["violations"]), result["violations"])

    def test_forbidden_prefix_path_denied(self) -> None:
        self.forbidden({"core/hosted_control/ops.py": (b"x = 1\n", "100644")}, "forbidden prefix")

    def test_forbidden_suffix_denied(self) -> None:
        self.forbidden({"core/api/leak.pem": (b"key\n", "100644")}, "forbidden suffix")

    def test_forbidden_component_denied(self) -> None:
        self.forbidden({"core/api/secrets/config.py": (b"x = 1\n", "100644")}, "forbidden component")

    def test_secret_marker_content_denied(self) -> None:
        self.forbidden(
            {"core/api/note.md": (b"MARVIS_PRIVATE_SENTINEL_DO_NOT_EXPORT\n", "100644")},
            "forbidden content marker",
        )

    def test_credential_shaped_content_denied(self) -> None:
        self.forbidden(
            {"core/api/settings_example.py": (b'TOKEN = "AKIAIOSFODNN7EXAMPLE"\n', "100644")},
            "credential-shaped",
        )

    def test_unguarded_forbidden_import_denied(self) -> None:
        self.forbidden(
            {"core/api/billing.py": (b"import workos\n", "100644")},
            "unguarded forbidden import",
        )

    def test_guarded_forbidden_import_reported_as_seam(self) -> None:
        content = (
            b"try:\n"
            b"    import workos\n"
            b"except ImportError:\n"
            b"    workos = None\n"
        )
        bundle = build_bundle(self.base, {"core/api/optional.py": (content, "100644")})
        exit_code = isp.run(make_args(self.repo, bundle))
        self.assertEqual(exit_code, 0)
        state = isp.load_bundle(bundle, bundle_expectations(bundle))
        ownership, _ = isp.load_ownership_map(self.repo / "contracts/shared-ownership.yaml")
        result = isp.classify(state, ownership, self.repo)
        self.assertEqual(result["violations"], [])
        self.assertEqual(result["optional_integration_seams"], [{"path": "core/api/optional.py", "line": 2, "module": "workos"}])

    # -- ownership ---------------------------------------------------------

    def test_oss_owned_collision_blocks_and_apply_refuses(self) -> None:
        files = dict(green_files())
        files["README.md"] = (b"upstream readme\n", "100644")
        files["pyproject.toml"] = (b'[project]\nname = "upstream"\n', "100644")
        bundle = build_bundle(self.base, files)
        exit_code = isp.run(make_args(self.repo, bundle))
        self.assertEqual(exit_code, 2)
        blocked_paths = {item["path"]: item for item in self._blocked(bundle)}
        self.assertIn("README.md", blocked_paths)
        self.assertEqual(blocked_paths["README.md"]["owner"], "marvis")
        self.assertIn("pyproject.toml", blocked_paths)

    def test_oss_owned_wins_over_managed_prefix(self) -> None:
        files = dict(green_files())
        files["core/api/tests/test_engine.py"] = (b"def test():\n    assert False\n", "100644")
        bundle = build_bundle(self.base, files)
        blocked_paths = {item["path"] for item in self._blocked(bundle)}
        self.assertIn("core/api/tests/test_engine.py", blocked_paths)

    def test_approved_oss_owned_path_is_preserved_on_apply(self) -> None:
        ownership_path = self.repo / "contracts/shared-ownership.yaml"
        ownership_path.write_text(
            TEST_MAP.replace("approved_preserve_paths: []", "approved_preserve_paths:\n  - README.md"),
            encoding="utf-8",
        )
        _git(self.repo, "add", str(ownership_path))
        _git(self.repo, "commit", "-q", "-m", "approve preserve")
        files = dict(green_files())
        files["README.md"] = (b"upstream readme\n", "100644")
        bundle = build_bundle(self.base, files)
        report_path = self.base / "preserve-report.json"
        self.assertEqual(isp.run(make_args(self.repo, bundle, report=report_path)), 0)
        report = json.loads(report_path.read_text(encoding="utf-8"))
        self.assertEqual(report["classification"]["preserved_oss_owned"], ["README.md"])
        backup = self.base / "preserve-backup"
        apply_report_path = self.base / "preserve-apply-report.json"
        self.assertEqual(
            isp.run(
                make_args(
                    self.repo,
                    bundle,
                    mode="apply",
                    backup_dir=backup,
                    report=apply_report_path,
                )
            ),
            0,
        )
        self.assertEqual((self.repo / "README.md").read_bytes(), b"oss readme\n")
        apply_report = json.loads(apply_report_path.read_text(encoding="utf-8"))
        self.assertNotEqual(
            apply_report["applied"]["readback_imported_files_sha256"],
            apply_report["identities"]["payload_sha256"],
        )

    def test_preserve_path_outside_oss_ownership_is_invalid(self) -> None:
        ownership_path = self.repo / "contracts/shared-ownership.yaml"
        ownership_path.write_text(
            TEST_MAP.replace(
                "approved_preserve_paths: []",
                "approved_preserve_paths:\n  - core/api/engine.py",
            ),
            encoding="utf-8",
        )
        with self.assertRaises(isp.ImportRefused):
            isp.load_ownership_map(ownership_path)

    def test_undeclared_path_outside_managed_areas_blocks(self) -> None:
        bundle = build_bundle(self.base, {"docs/decisions/adr.md": (b"# overwritten\n", "100644")})
        blocked_paths = {item["path"]: item for item in self._blocked(bundle)}
        self.assertIn("docs/decisions/adr.md", blocked_paths)
        self.assertEqual(blocked_paths["docs/decisions/adr.md"]["owner"], "undeclared")

    def test_migration_history_change_blocks(self) -> None:
        files = dict(green_files())
        files["migrations/001_init.py"] = (b"# rewritten history\n", "100644")
        bundle = build_bundle(self.base, files)
        exit_code = isp.run(make_args(self.repo, bundle))
        self.assertEqual(exit_code, 2)
        state = isp.load_bundle(bundle, bundle_expectations(bundle))
        ownership, _ = isp.load_ownership_map(self.repo / "contracts/shared-ownership.yaml")
        result = isp.classify(state, ownership, self.repo)
        self.assertTrue(any("migration compatibility" in v for v in result["violations"]))

    def test_real_ownership_map_loads(self) -> None:
        repo_root = Path(__file__).resolve().parent.parent
        ownership, _ = isp.load_ownership_map(repo_root / "contracts/shared-ownership.yaml")
        self.assertEqual(ownership["ownership_map_version"], 2)
        self.assertIn("core/api/", ownership["managed_areas"])

    # -- apply / rollback --------------------------------------------------

    def test_apply_writes_byte_identical_files_and_rolls_back(self) -> None:
        bundle = build_bundle(self.base, green_files())
        backup = self.base / "backup"
        exit_code = isp.run(make_args(self.repo, bundle, mode="apply", backup_dir=backup))
        self.assertEqual(exit_code, 0)
        self.assertEqual((self.repo / "core/api/engine.py").read_bytes(), b"ENGINE = 2\n")
        self.assertEqual((self.repo / "core/api/new_module.py").read_bytes(), b"NEW = True\n")
        # never_delete: the local-only file survives the apply untouched.
        self.assertEqual((self.repo / "core/api/local_only.py").read_bytes(), b"LOCAL = True\n")
        rollback_code = isp.run(make_args(self.repo, bundle, mode="rollback", backup_dir=backup))
        self.assertEqual(rollback_code, 0)
        self.assertEqual((self.repo / "core/api/engine.py").read_bytes(), b"ENGINE = 1\n")
        self.assertFalse((self.repo / "core/api/new_module.py").exists())
        self.assertTrue((self.repo / "core/api/local_only.py").exists())

    def test_apply_preserves_executable_mode(self) -> None:
        files = dict(green_files())
        files["core/api/runner.py"] = (b"#!/usr/bin/env python3\n", "100755")
        bundle = build_bundle(self.base, files)
        backup = self.base / "backup"
        exit_code = isp.run(make_args(self.repo, bundle, mode="apply", backup_dir=backup))
        self.assertEqual(exit_code, 0)
        self.assertTrue((self.repo / "core/api/runner.py").stat().st_mode & 0o111)

    def test_apply_refuses_dirty_worktree(self) -> None:
        bundle = build_bundle(self.base, green_files())
        (self.repo / "README.md").write_text("dirty\n", encoding="utf-8")
        self.refused(make_args(self.repo, bundle, mode="apply", backup_dir=self.base / "backup"))

    def test_apply_refuses_existing_backup_dir(self) -> None:
        bundle = build_bundle(self.base, green_files())
        backup = self.base / "backup"
        backup.mkdir()
        self.refused(make_args(self.repo, bundle, mode="apply", backup_dir=backup))

    def test_apply_refuses_backup_dir_inside_repo(self) -> None:
        bundle = build_bundle(self.base, green_files())
        self.refused(make_args(self.repo, bundle, mode="apply", backup_dir=self.repo / "backup"))

    def test_apply_refuses_symlinked_destination_directory(self) -> None:
        (self.repo / "core/api/sub").symlink_to("../..")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-q", "-m", "symlink")
        bundle = build_bundle(self.base, {"core/api/sub/evil.py": (b"x = 1\n", "100644")})
        self.refused(make_args(self.repo, bundle, mode="apply", backup_dir=self.base / "backup"))
        self.assertFalse((self.repo / "core" / "evil.py").exists())

    def test_rollback_refuses_drifted_state(self) -> None:
        bundle = build_bundle(self.base, green_files())
        backup = self.base / "backup"
        isp.run(make_args(self.repo, bundle, mode="apply", backup_dir=backup))
        (self.repo / "core/api/engine.py").write_bytes(b"ENGINE = 42\n")
        self.refused(make_args(self.repo, bundle, mode="rollback", backup_dir=backup))

    # -- helpers -----------------------------------------------------------

    def _blocked(self, bundle: Path) -> list[dict]:
        state = isp.load_bundle(bundle, bundle_expectations(bundle))
        ownership, _ = isp.load_ownership_map(self.repo / "contracts/shared-ownership.yaml")
        return isp.classify(state, ownership, self.repo)["blocked"]


if __name__ == "__main__":
    unittest.main()
