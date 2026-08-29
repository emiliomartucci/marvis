from __future__ import annotations

import hashlib
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
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

    def test_cli_upgrade_uses_settings_projects_root_without_environment(self) -> None:
        from typer.testing import CliRunner

        from core.api import db as db_mod
        from core.api import config as config_mod
        from core.api import runtime_settings
        from core.api.config import settings
        from core.api.routers import projects as projects_router
        from core.api.services import schema_upgrade
        from core.cli.marvis_init import app

        with tempfile.TemporaryDirectory(prefix="marvis-schema-cli-") as raw:
            root = Path(raw)
            projects_root = root / "configured-projects"
            project_dir = projects_root / "sample"
            project_dir.mkdir(parents=True)
            (project_dir / "project.yaml").write_text(
                "name: Sample\nslug: sample\ntype: work\nlifecycle: active\n",
                encoding="utf-8",
            )
            database = root / "configured.db"
            settings_path = root / "settings.yaml"
            settings_path.write_text(
                "storage:\n"
                f"  db_path: {database}\n"
                f"  projects_root: {projects_root}\n",
                encoding="utf-8",
            )
            prior_migrations = root / "prior-migrations"
            prior_migrations.mkdir()
            for migration in db_mod.discover_up_migrations(ROOT / "migrations"):
                if db_mod._migration_version(migration) < 187:
                    shutil.copy2(migration, prior_migrations / migration.name)

            original_migrations = db_mod.MIGRATIONS_DIR
            original_db_path = settings.db_path
            original_backup_dir = settings.db_backup_dir
            original_settings_path = os.environ.get("MARVIS_SETTINGS_PATH")
            original_projects_root = os.environ.get("MARVIS_PROJECTS_ROOT")
            original_password = os.environ.get("PIR_PASSWORD")
            original_applied = runtime_settings._applied
            original_project_dirs = list(projects_router.PROJECT_DIRS)
            original_repo_parents = list(config_mod.ALLOWED_REPO_PARENTS)
            try:
                settings.db_path = str(database)
                settings.db_backup_dir = str(root / "backups")
                os.environ["MARVIS_PROJECTS_ROOT"] = str(projects_root)
                os.environ.setdefault("PIR_PASSWORD", "test-migration-seed-password")
                db_mod.MIGRATIONS_DIR = prior_migrations
                prior = db_mod.run_migrations()
                self.assertEqual(prior.final_version, 186)
                with sqlite3.connect(database) as connection:
                    connection.execute(
                        "INSERT INTO tasks"
                        "(id,title,status,project,priority,created_by,source,workspace_id) "
                        "VALUES(?,?,?,?,?,?,?,?)",
                        (
                            "cli-upgrade-fixture",
                            "CLI upgrade fixture",
                            "pending",
                            "sample",
                            "medium",
                            "local",
                            "fixture",
                            "ws_default",
                        ),
                    )
                    connection.commit()

                db_mod.MIGRATIONS_DIR = original_migrations
                os.environ["MARVIS_SETTINGS_PATH"] = str(settings_path)
                os.environ.pop("MARVIS_PROJECTS_ROOT", None)
                runtime_settings._applied = False
                receipt = root / "receipt.json"
                with patch.object(
                    schema_upgrade,
                    "prove_local_writers_stopped",
                    return_value="test_process_scan",
                ):
                    result = CliRunner().invoke(
                        app,
                        [
                            "schema",
                            "upgrade",
                            "--release-id",
                            "marvis-oss@test-settings-root",
                            "--receipt",
                            str(receipt),
                            "--json",
                        ],
                    )
                self.assertEqual(result.exit_code, 0, result.output)
                self.assertEqual(
                    os.environ.get("MARVIS_PROJECTS_ROOT"),
                    str(projects_root.resolve()),
                )
                with sqlite3.connect(database) as connection:
                    version = connection.execute(
                        "SELECT MAX(version) FROM schema_versions"
                    ).fetchone()[0]
                    lifecycle = connection.execute(
                        "SELECT lifecycle FROM project_lifecycle_state "
                        "WHERE workspace_id=? AND project_slug=?",
                        ("ws_default", "sample"),
                    ).fetchone()
                self.assertEqual(version, 187)
                self.assertEqual(lifecycle, ("active",))
            finally:
                db_mod.MIGRATIONS_DIR = original_migrations
                settings.db_path = original_db_path
                settings.db_backup_dir = original_backup_dir
                runtime_settings._applied = original_applied
                projects_router._set_project_dirs(original_project_dirs)
                config_mod.ALLOWED_REPO_PARENTS[:] = original_repo_parents
                if original_settings_path is None:
                    os.environ.pop("MARVIS_SETTINGS_PATH", None)
                else:
                    os.environ["MARVIS_SETTINGS_PATH"] = original_settings_path
                if original_projects_root is None:
                    os.environ.pop("MARVIS_PROJECTS_ROOT", None)
                else:
                    os.environ["MARVIS_PROJECTS_ROOT"] = original_projects_root
                if original_password is None:
                    os.environ.pop("PIR_PASSWORD", None)
                else:
                    os.environ["PIR_PASSWORD"] = original_password


if __name__ == "__main__":
    unittest.main()
