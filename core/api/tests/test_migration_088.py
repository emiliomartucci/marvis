"""Migration 088: rename last_context_pct → last_context_pct_legacy."""
from __future__ import annotations

import sqlite3

import pytest

from core.api.paths import repo_path

MIGRATIONS_DIR = repo_path(__file__, "migrations")


def _seed_post_087_schema(conn: sqlite3.Connection) -> None:
    """Seed schema as it stands after migration 087 (dual metrics columns).

    Only the columns relevant to 088 are modelled here; the real table has
    many more but they're orthogonal to the rename.
    """
    conn.executescript(
        """
        CREATE TABLE schema_versions (version INTEGER PRIMARY KEY);
        CREATE TABLE sessions_meta (
            name TEXT PRIMARY KEY,
            last_cost_usd REAL,
            last_context_pct REAL,
            last_context_pct_real REAL,
            last_context_pct_scaled REAL
        );
        INSERT INTO schema_versions (version) VALUES (87);
        INSERT INTO sessions_meta (name, last_context_pct, last_context_pct_real)
        VALUES ('sess-one', 42.0, 31.5);
        """
    )
    conn.commit()


def _apply(conn: sqlite3.Connection, name: str) -> None:
    path = MIGRATIONS_DIR / name
    assert path.exists(), f"Migration file {name} missing"
    conn.executescript(path.read_text())
    conn.commit()


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


@pytest.fixture
def fresh_db(tmp_path):
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    _seed_post_087_schema(conn)
    try:
        yield conn
    finally:
        conn.close()


def test_up_renames_column_and_preserves_data(fresh_db):
    cols_before = _columns(fresh_db, "sessions_meta")
    assert "last_context_pct" in cols_before
    assert "last_context_pct_legacy" not in cols_before

    _apply(fresh_db, "088_rename_context_pct_legacy.sql")

    cols_after = _columns(fresh_db, "sessions_meta")
    assert "last_context_pct_legacy" in cols_after
    assert "last_context_pct" not in cols_after
    # _real and _scaled are untouched
    assert "last_context_pct_real" in cols_after
    assert "last_context_pct_scaled" in cols_after

    # Data survives the rename
    value = fresh_db.execute(
        "SELECT last_context_pct_legacy FROM sessions_meta WHERE name = 'sess-one'"
    ).fetchone()[0]
    assert value == 42.0

    # schema_versions bumped
    versions = {row[0] for row in fresh_db.execute("SELECT version FROM schema_versions")}
    assert 88 in versions


def test_down_reverts_rename(fresh_db):
    _apply(fresh_db, "088_rename_context_pct_legacy.sql")
    _apply(fresh_db, "088_rename_context_pct_legacy_down.sql")

    cols = _columns(fresh_db, "sessions_meta")
    assert "last_context_pct" in cols
    assert "last_context_pct_legacy" not in cols

    versions = {row[0] for row in fresh_db.execute("SELECT version FROM schema_versions")}
    assert 88 not in versions
    assert 87 in versions
