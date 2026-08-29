#!/usr/bin/env python3
"""Verify immutable prior-wheel upgrade, backup, denial and atomic rollback."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from email.parser import Parser
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
from typing import Any
import urllib.request
import zipfile


DEFAULT_ROOT = Path(__file__).resolve().parents[1]
if str(DEFAULT_ROOT) not in sys.path:
    sys.path.insert(0, str(DEFAULT_ROOT))


CONTRACT_SCHEMA = "marvis-prior-distributions/v1"
BACKUP_MANIFEST_SCHEMA = "marvis-local-backup-manifest/v1"
REPORT_SCHEMA = "marvis-local-upgrade-verification/v1"
_SURFACE_PATHS = {
    "database": Path("console.db"),
    "settings": Path("vault/settings.yaml"),
    "project_tree": Path("projects"),
    "vault_state": Path("vault/byok.vault"),
    "hook_state": Path("project/.claude"),
}


class UpgradeVerificationError(RuntimeError):
    pass


@dataclass(frozen=True)
class PriorDistribution:
    version: str
    role: str
    filename: str
    sha256: str
    url: str


def _sha_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _sha_file(path: Path) -> str:
    return _sha_bytes(path.read_bytes())


def _write_json(path: Path, value: Any) -> str:
    raw = (json.dumps(value, sort_keys=True, indent=2) + "\n").encode("utf-8")
    path.write_bytes(raw)
    return _sha_bytes(raw)


def _tree_digest(path: Path) -> str:
    digest = hashlib.sha256()
    if not path.exists():
        return _sha_bytes(b"absent\n")
    if path.is_symlink():
        raise UpgradeVerificationError(f"surface symlink rejected: {path.name}")
    if path.is_file():
        return _sha_file(path)
    for item in sorted(path.rglob("*"), key=lambda value: value.relative_to(path).as_posix()):
        relative = item.relative_to(path).as_posix()
        if item.is_symlink():
            raise UpgradeVerificationError(f"surface symlink rejected: {relative}")
        if item.is_dir():
            digest.update(f"dir\0{relative}\n".encode())
        elif item.is_file():
            digest.update(f"file\0{relative}\0{item.stat().st_mode & 0o777:o}\0".encode())
            digest.update(item.read_bytes())
            digest.update(b"\n")
    return digest.hexdigest()


def _logical_database_digest(path: Path) -> str:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        check = connection.execute("PRAGMA integrity_check").fetchone()
        if not check or str(check[0]).lower() != "ok":
            raise UpgradeVerificationError("database integrity red")
        lines = [line for line in connection.iterdump() if not line.startswith("BEGIN TRANSACTION")]
    finally:
        connection.close()
    return _sha_bytes(("\n".join(lines) + "\n").encode("utf-8"))


def _load_contract(path: Path) -> list[PriorDistribution]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise UpgradeVerificationError("prior distribution contract invalid") from exc
    if payload.get("schema") != CONTRACT_SCHEMA or not isinstance(payload.get("supported"), list):
        raise UpgradeVerificationError("prior distribution contract unsupported")
    result = []
    for row in payload["supported"]:
        result.append(PriorDistribution(**row))
    if not result:
        raise UpgradeVerificationError("no supported prior distribution")
    return result


def _download(prior: PriorDistribution, directory: Path) -> Path:
    target = directory / prior.filename
    with urllib.request.urlopen(prior.url, timeout=30) as response:  # noqa: S310 - pinned PyPI URL + digest
        target.write_bytes(response.read())
    return target


def _wheel_version_and_migrations(wheel: Path, destination: Path) -> tuple[str, Path]:
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        metadata_names = [name for name in names if name.endswith(".dist-info/METADATA")]
        if len(metadata_names) != 1:
            raise UpgradeVerificationError("prior wheel metadata inventory invalid")
        metadata = Parser().parsestr(archive.read(metadata_names[0]).decode("utf-8"))
        version = str(metadata.get("Version") or "")
        selected = [
            name
            for name in names
            if name.startswith("migrations/") and name.endswith(".sql")
        ]
        if not selected or any(".." in Path(name).parts for name in selected):
            raise UpgradeVerificationError("prior wheel migration inventory invalid")
        for name in selected:
            target = destination / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(archive.read(name))
    return version, destination / "migrations"


def _surface_manifest(root: Path, artifact: PriorDistribution, artifact_sha: str) -> dict[str, Any]:
    entries = []
    for name, relative_path in _SURFACE_PATHS.items():
        path = root / relative_path
        entries.append(
            {
                "surface": name,
                "initial_sha256": (
                    _logical_database_digest(path)
                    if name == "database"
                    else _tree_digest(path)
                ),
                "strategy": (
                    "sqlite_verified_backup"
                    if name == "database"
                    else "digest_invariant_not_mutated_by_schema_upgrade"
                ),
            }
        )
    return {
        "schema": BACKUP_MANIFEST_SCHEMA,
        "prior_distribution": {
            "version": artifact.version,
            "filename": artifact.filename,
            "sha256": artifact_sha,
        },
        "entries": entries,
        "exclusions": [
            {
                "surface": "package_environment",
                "reason": "package manager owned; prior pin is allowed only after data restore verification",
            },
            {
                "surface": "model_cache",
                "reason": "immutable cache; schema upgrade does not mutate or delete it",
            },
        ],
        "rollback_order": [
            "restore_verified_database_backup",
            "verify_schema_data_and_invariant_surfaces",
            "allow_prior_package_pin",
        ],
    }


def _surface_digests(root: Path) -> dict[str, str]:
    return {
        name: _tree_digest(root / relative_path)
        for name, relative_path in _SURFACE_PATHS.items()
        if name != "database"
    }


def _assert_invariants(expected: dict[str, str], observed: dict[str, str]) -> None:
    if observed != expected:
        changed = sorted(key for key in expected if expected.get(key) != observed.get(key))
        raise UpgradeVerificationError(f"non-database surface mutated: {changed}")


def _seed_surfaces(root: Path) -> None:
    (root / "vault").mkdir()
    (root / "projects/sample").mkdir(parents=True)
    (root / "project/.claude/hooks").mkdir(parents=True)
    (root / "vault/settings.yaml").write_text(
        "storage:\n"
        f"  db_path: {root / 'console.db'}\n"
        f"  projects_root: {root / 'projects'}\n",
        encoding="utf-8",
    )
    (root / "vault/byok.vault").write_bytes(b"synthetic-encrypted-vault-fixture\n")
    (root / "projects/sample/project.yaml").write_text(
        "name: Synthetic Project\nslug: sample\ntype: work\n", encoding="utf-8"
    )
    (root / "project/.claude/settings.json").write_text(
        '{"hooks":{"PreToolUse":[]}}\n', encoding="utf-8"
    )
    (root / "project/.claude/hooks/synthetic.sh").write_text(
        "#!/bin/sh\nexit 0\n", encoding="utf-8"
    )


def verify_upgrade(
    repo_root: Path,
    wheel: Path,
    prior: PriorDistribution,
    *,
    evidence_dir: Path | None = None,
) -> dict[str, Any]:
    artifact_sha = _sha_file(wheel)
    if artifact_sha != prior.sha256 or wheel.name != prior.filename:
        raise UpgradeVerificationError("prior distribution identity mismatch")

    from core.api import db as db_mod
    from core.api import config as config_mod
    from core.api.config import settings
    from core.api import runtime_settings
    from core.api.routers import projects as projects_router
    from core.api.services import schema_upgrade

    with tempfile.TemporaryDirectory(prefix="marvis-upgrade-") as raw:
        root = Path(raw)
        extracted = root / "prior-wheel"
        version, prior_migrations = _wheel_version_and_migrations(wheel, extracted)
        if version != prior.version:
            raise UpgradeVerificationError("prior wheel version mismatch")
        _seed_surfaces(root)

        original_migrations = db_mod.MIGRATIONS_DIR
        original_db_path = settings.db_path
        original_backup_dir = settings.db_backup_dir
        original_quiesced = os.environ.get(db_mod.QUIESCED_MIGRATION_ENV)
        original_projects_root = os.environ.get("MARVIS_PROJECTS_ROOT")
        original_settings_path = os.environ.get("MARVIS_SETTINGS_PATH")
        original_settings_applied = runtime_settings._applied
        original_project_dirs = list(projects_router.PROJECT_DIRS)
        original_repo_parents = list(config_mod.ALLOWED_REPO_PARENTS)
        database = root / "console.db"
        receipt_path = root / "upgrade-receipt.json"
        try:
            os.environ["MARVIS_SETTINGS_PATH"] = str(root / "vault/settings.yaml")
            os.environ.pop("MARVIS_PROJECTS_ROOT", None)
            runtime_settings.apply_marvis_settings(force=True)
            if Path(settings.db_path) != database:
                raise UpgradeVerificationError("settings database path was not applied")
            if os.environ.get("MARVIS_PROJECTS_ROOT") != str(
                (root / "projects").resolve()
            ):
                raise UpgradeVerificationError("settings projects root was not applied")
            settings.db_backup_dir = str(root / "backups")
            os.environ[db_mod.QUIESCED_MIGRATION_ENV] = "1"
            db_mod.MIGRATIONS_DIR = prior_migrations
            prior_files = db_mod.discover_up_migrations()
            prior_max = db_mod.code_max_version(prior_files)
            prior_versions = {db_mod._migration_version(path) for path in prior_files}
            prior_result = db_mod.run_migrations()
            if prior_result.final_version != prior_max:
                raise UpgradeVerificationError("prior schema build incomplete")

            connection = sqlite3.connect(database)
            try:
                connection.execute(
                    "INSERT INTO tasks(id,title,status,project,priority,created_by,source,workspace_id) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (
                        "upgrade-fixture-task",
                        "Upgrade fixture survives",
                        "pending",
                        "sample",
                        "medium",
                        "local",
                        "fixture",
                        "ws_default",
                    ),
                )
                connection.commit()
            finally:
                connection.close()

            initial_logical = _logical_database_digest(database)
            invariant_digests = _surface_digests(root)
            manifest = _surface_manifest(root, prior, artifact_sha)
            manifest_path = root / "backup-manifest.json"
            manifest_sha = _write_json(manifest_path, manifest)

            db_mod.MIGRATIONS_DIR = repo_root / "migrations"
            current_files = db_mod.discover_up_migrations()
            current_max = db_mod.code_max_version(current_files)
            if current_max <= prior_max:
                raise UpgradeVerificationError("current schema is not newer than prior schema")
            upgraded = schema_upgrade.run_controlled_upgrade(
                "marvis-plan-b@candidate",
                proof_kind="immutable_prior_fixture",
                receipt_path=receipt_path,
            )
            if upgraded.status != "succeeded" or upgraded.final_version != current_max:
                raise UpgradeVerificationError("controlled upgrade did not reach current schema")
            _assert_invariants(invariant_digests, _surface_digests(root))

            connection = sqlite3.connect(database)
            try:
                row = connection.execute(
                    "SELECT title, workspace_id FROM tasks WHERE id=?",
                    ("upgrade-fixture-task",),
                ).fetchone()
                if row != ("Upgrade fixture survives", "ws_default"):
                    raise UpgradeVerificationError("synthetic data changed during upgrade")
                try:
                    db_mod.assert_schema_compatible(connection, prior_max, prior_versions)
                except RuntimeError as exc:
                    old_binary_denied = "OLDER image" in str(exc)
                else:
                    old_binary_denied = False
            finally:
                connection.close()
            if not old_binary_denied:
                raise UpgradeVerificationError("old binary accepted the forward schema")
            if not upgraded.backup_path:
                raise UpgradeVerificationError("upgrade produced no database backup")
            backup_sha = _sha_file(Path(upgraded.backup_path))

            rolled_back = schema_upgrade.restore_controlled_upgrade(
                "marvis-plan-b@candidate",
                proof_kind="immutable_prior_fixture",
                receipt_path=receipt_path,
            )
            if rolled_back.status != "rolled_back" or rolled_back.final_version != prior_max:
                raise UpgradeVerificationError("rollback did not restore prior schema")
            _assert_invariants(invariant_digests, _surface_digests(root))
            restored_logical = _logical_database_digest(database)
            if restored_logical != initial_logical:
                raise UpgradeVerificationError("rollback logical database digest mismatch")

            report = {
                "schema": REPORT_SCHEMA,
                "prior_version": prior.version,
                "prior_artifact_sha256": artifact_sha,
                "prior_schema_version": prior_max,
                "current_schema_version": current_max,
                "applied_versions": list(upgraded.applied_versions),
                "backup_manifest_sha256": manifest_sha,
                "database_backup_sha256": backup_sha,
                "invariant_surfaces": sorted(invariant_digests),
                "synthetic_data_preserved": True,
                "old_binary_forward_schema": "deny",
                "rollback_status": rolled_back.status,
                "rollback_logical_digest_restored": True,
                "package_pin_performed": False,
                "package_pin_gate": "allowed_only_after_restore_verification",
            }
            if evidence_dir is not None:
                evidence_dir.mkdir(parents=True, exist_ok=True)
                shutil.copy2(manifest_path, evidence_dir / "backup-manifest.json")
                _write_json(evidence_dir / "upgrade-report.json", report)
            return report
        finally:
            db_mod.MIGRATIONS_DIR = original_migrations
            settings.db_path = original_db_path
            settings.db_backup_dir = original_backup_dir
            if original_quiesced is None:
                os.environ.pop(db_mod.QUIESCED_MIGRATION_ENV, None)
            else:
                os.environ[db_mod.QUIESCED_MIGRATION_ENV] = original_quiesced
            if original_projects_root is None:
                os.environ.pop("MARVIS_PROJECTS_ROOT", None)
            else:
                os.environ["MARVIS_PROJECTS_ROOT"] = original_projects_root
            if original_settings_path is None:
                os.environ.pop("MARVIS_SETTINGS_PATH", None)
            else:
                os.environ["MARVIS_SETTINGS_PATH"] = original_settings_path
            runtime_settings._applied = original_settings_applied
            projects_router._set_project_dirs(original_project_dirs)
            config_mod.ALLOWED_REPO_PARENTS[:] = original_repo_parents


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--artifact", type=Path)
    parser.add_argument("--download", action="store_true")
    parser.add_argument("--version", default="0.3.8")
    parser.add_argument("--evidence-dir", type=Path)
    args = parser.parse_args(argv)
    root = args.root.resolve()
    try:
        priors = _load_contract(root / "contracts/compatibility/prior-distributions-v1.json")
        prior = next((item for item in priors if item.version == args.version), None)
        if prior is None:
            raise UpgradeVerificationError("requested prior version is not supported")
        if args.artifact is not None:
            artifact = args.artifact.resolve()
            report = verify_upgrade(root, artifact, prior, evidence_dir=args.evidence_dir)
        elif args.download:
            with tempfile.TemporaryDirectory(prefix="marvis-prior-download-") as raw:
                artifact = _download(prior, Path(raw))
                report = verify_upgrade(root, artifact, prior, evidence_dir=args.evidence_dir)
        else:
            raise UpgradeVerificationError("pass --artifact or --download")
    except (UpgradeVerificationError, OSError, sqlite3.Error, zipfile.BadZipFile) as exc:
        print(f"local upgrade: FAIL: {exc}")
        return 1
    print("local upgrade: PASS " + json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
