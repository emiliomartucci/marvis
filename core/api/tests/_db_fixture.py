"""Test-only helpers that keep SQLite fixtures on the production schema path."""
from __future__ import annotations

import os


def apply_migrations(db_path: str) -> None:
    """Build a fresh test DB through the same SQL and Python hooks as runtime."""
    from core.api import db as db_module

    previous_db_path = db_module.settings.db_path
    previous_password = os.environ.get("PIR_PASSWORD")
    has_admin_password = bool(
        os.environ.get("PIR_ADMIN_PASSWORD_HASH", "").strip()
        or os.environ.get("PIR_PASSWORD", "").strip()
    )
    try:
        db_module.settings.db_path = db_path
        if not has_admin_password:
            os.environ["PIR_PASSWORD"] = "test-migration-seed-password"
        db_module.run_migrations()
    finally:
        db_module.settings.db_path = previous_db_path
        if not has_admin_password:
            if previous_password is None:
                os.environ.pop("PIR_PASSWORD", None)
            else:
                os.environ["PIR_PASSWORD"] = previous_password
