from __future__ import annotations

import sqlite3

from core.api import db as db_module
from core.api.db import _add_session_theme_mode_column


def test_add_session_theme_mode_column_is_idempotent() -> None:
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("CREATE TABLE sessions_meta (name TEXT PRIMARY KEY)")

        _add_session_theme_mode_column(conn)
        _add_session_theme_mode_column(conn)

        columns = [row[1] for row in conn.execute("PRAGMA table_info(sessions_meta)")]
        assert "theme_mode" in columns
        assert columns.count("theme_mode") == 1
    finally:
        conn.close()


def test_migration_057_marks_schema_current_when_column_already_exists(
    tmp_path,
    monkeypatch,
) -> None:
    db_path = tmp_path / "migration-057-existing-column.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "CREATE TABLE schema_versions (version INTEGER PRIMARY KEY, applied_at DATETIME DEFAULT CURRENT_TIMESTAMP)"
        )
        conn.execute(
            "CREATE TABLE sessions_meta (name TEXT PRIMARY KEY, theme_mode TEXT DEFAULT NULL)"
        )
        conn.execute("INSERT INTO schema_versions (version) VALUES (56)")
        conn.commit()
    finally:
        conn.close()

    monkeypatch.setattr(db_module.settings, "db_path", str(db_path))

    db_module.run_migrations()

    conn = sqlite3.connect(db_path)
    try:
        columns = [row[1] for row in conn.execute("PRAGMA table_info(sessions_meta)")]
        latest_version = conn.execute(
            "SELECT version FROM schema_versions ORDER BY version DESC LIMIT 1"
        ).fetchone()[0]
    finally:
        conn.close()

    assert columns.count("theme_mode") == 1
    assert latest_version == 57
