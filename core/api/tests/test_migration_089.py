"""Migration 089: shadow cost_equivalent columns."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from core.api.paths import repo_path

MIGRATIONS_DIR = repo_path(__file__, "migrations")


def _seed_minimum_schema(conn: sqlite3.Connection) -> None:
    """Minimal precursor schema with sessions_meta + schema_versions at v88."""
    conn.executescript(
        """
        CREATE TABLE schema_versions (version INTEGER PRIMARY KEY);
        CREATE TABLE sessions_meta (
            name TEXT PRIMARY KEY,
            last_cost_usd REAL,
            last_context_pct REAL
        );
        INSERT INTO schema_versions (version) VALUES (88);
        """
    )
    conn.commit()


def _apply_sql(conn: sqlite3.Connection, path: Path) -> None:
    conn.executescript(path.read_text())
    conn.commit()


@pytest.fixture
def fresh_db(tmp_path):
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    _seed_minimum_schema(conn)
    yield conn, db_path
    conn.close()


def _column_names(conn: sqlite3.Connection, table: str) -> set[str]:
    cursor = conn.execute(f"PRAGMA table_info({table})")
    return {row["name"] for row in cursor.fetchall()}


def test_up_migration_adds_equivalent_columns(fresh_db):
    conn, _ = fresh_db
    up = MIGRATIONS_DIR / "089_session_metrics_equivalent_cost.sql"
    assert up.exists(), "Migration 089 up file missing"
    _apply_sql(conn, up)

    cols = _column_names(conn, "sessions_meta")
    for expected in (
        "last_cost_conversation_equivalent_usd",
        "last_cost_session_equivalent_usd",
        "last_cost_equivalent_pricing_version",
    ):
        assert expected in cols, f"missing column {expected!r}"

    # schema_versions bumped to 89
    cursor = conn.execute("SELECT MAX(version) FROM schema_versions")
    assert cursor.fetchone()[0] == 89


def test_up_migration_columns_accept_null_and_values(fresh_db):
    conn, _ = fresh_db
    up = MIGRATIONS_DIR / "089_session_metrics_equivalent_cost.sql"
    _apply_sql(conn, up)

    conn.execute("INSERT INTO sessions_meta (name) VALUES ('demo')")
    conn.execute(
        "UPDATE sessions_meta SET "
        "last_cost_conversation_equivalent_usd = ?, "
        "last_cost_session_equivalent_usd = ?, "
        "last_cost_equivalent_pricing_version = ? "
        "WHERE name = ?",
        (12.5, 15.3, "2026-04-23", "demo"),
    )
    conn.commit()

    row = conn.execute(
        "SELECT last_cost_conversation_equivalent_usd, "
        "last_cost_session_equivalent_usd, "
        "last_cost_equivalent_pricing_version "
        "FROM sessions_meta WHERE name = 'demo'"
    ).fetchone()
    assert row["last_cost_conversation_equivalent_usd"] == pytest.approx(12.5)
    assert row["last_cost_session_equivalent_usd"] == pytest.approx(15.3)
    assert row["last_cost_equivalent_pricing_version"] == "2026-04-23"


def test_down_migration_drops_columns(fresh_db):
    conn, _ = fresh_db
    up = MIGRATIONS_DIR / "089_session_metrics_equivalent_cost.sql"
    down = MIGRATIONS_DIR / "089_session_metrics_equivalent_cost_down.sql"
    assert down.exists(), "Migration 089 down file missing"
    _apply_sql(conn, up)

    assert "last_cost_conversation_equivalent_usd" in _column_names(
        conn, "sessions_meta"
    )

    _apply_sql(conn, down)

    cols = _column_names(conn, "sessions_meta")
    for dropped in (
        "last_cost_conversation_equivalent_usd",
        "last_cost_session_equivalent_usd",
        "last_cost_equivalent_pricing_version",
    ):
        assert dropped not in cols, f"column {dropped!r} still present after down"

    cursor = conn.execute("SELECT MAX(version) FROM schema_versions")
    assert cursor.fetchone()[0] == 88
