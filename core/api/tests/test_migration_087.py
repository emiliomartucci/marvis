"""Migration 087: dual metrics columns + session_conversations table."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent.parent / "migrations"


def _seed_minimum_schema(conn: sqlite3.Connection) -> None:
    """Minimal precursor schema: a sessions_meta table + schema_versions."""
    conn.executescript(
        """
        CREATE TABLE schema_versions (version INTEGER PRIMARY KEY);
        CREATE TABLE sessions_meta (
            name TEXT PRIMARY KEY,
            last_cost_usd REAL,
            last_context_pct REAL
        );
        INSERT INTO schema_versions (version) VALUES (86);
        """
    )
    conn.commit()


def _apply_sql(conn: sqlite3.Connection, path: Path) -> None:
    conn.executescript(path.read_text())
    conn.execute("PRAGMA foreign_keys=ON")
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


def test_up_migration_adds_all_columns(fresh_db):
    conn, _ = fresh_db
    up = MIGRATIONS_DIR / "087_session_metrics_dual.sql"
    assert up.exists(), "Migration 087 up file missing"
    _apply_sql(conn, up)

    cols = _column_names(conn, "sessions_meta")
    for expected in (
        "last_context_pct_real",
        "last_context_pct_scaled",
        "last_cost_conversation_usd",
        "last_cost_session_usd",
        "last_cost_session_incomplete",
        "last_input_tokens",
        "last_output_tokens",
        "last_reasoning_tokens",
        "working_seconds_msg",
        "metrics_refreshed_at",
        "pricing_version",
    ):
        assert expected in cols, f"missing column {expected!r}"


def test_up_migration_creates_session_conversations_table(fresh_db):
    conn, _ = fresh_db
    up = MIGRATIONS_DIR / "087_session_metrics_dual.sql"
    _apply_sql(conn, up)

    cols = _column_names(conn, "session_conversations")
    assert cols == {"session_name", "conversation_id", "ord", "created_at"}

    # Indexes exist
    cursor = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='session_conversations'"
    )
    idx_names = {r["name"] for r in cursor.fetchall()}
    assert "idx_session_conv_name" in idx_names
    assert "idx_session_conv_id" in idx_names


def test_session_conversations_fk_cascade(fresh_db):
    conn, _ = fresh_db
    up = MIGRATIONS_DIR / "087_session_metrics_dual.sql"
    _apply_sql(conn, up)
    conn.execute("PRAGMA foreign_keys=ON")

    conn.execute("INSERT INTO sessions_meta (name) VALUES ('demo')")
    conn.execute(
        "INSERT INTO session_conversations (session_name, conversation_id, ord, created_at) "
        "VALUES (?, ?, ?, datetime('now'))",
        ("demo", "ses_a", 0),
    )
    conn.commit()

    # Delete parent → children cascade
    conn.execute("DELETE FROM sessions_meta WHERE name = 'demo'")
    conn.commit()
    cursor = conn.execute(
        "SELECT COUNT(*) as c FROM session_conversations WHERE session_name=?", ("demo",)
    )
    assert cursor.fetchone()["c"] == 0


def test_session_conversations_pk_dedupes(fresh_db):
    conn, _ = fresh_db
    up = MIGRATIONS_DIR / "087_session_metrics_dual.sql"
    _apply_sql(conn, up)

    conn.execute("INSERT INTO sessions_meta (name) VALUES ('demo')")
    conn.execute(
        "INSERT OR IGNORE INTO session_conversations "
        "(session_name, conversation_id, ord, created_at) VALUES (?, ?, ?, datetime('now'))",
        ("demo", "ses_a", 0),
    )
    conn.execute(
        "INSERT OR IGNORE INTO session_conversations "
        "(session_name, conversation_id, ord, created_at) VALUES (?, ?, ?, datetime('now'))",
        ("demo", "ses_a", 1),
    )
    conn.commit()
    cursor = conn.execute("SELECT COUNT(*) as c FROM session_conversations")
    assert cursor.fetchone()["c"] == 1


def test_down_migration_drops_columns_and_table(fresh_db):
    conn, _ = fresh_db
    up = MIGRATIONS_DIR / "087_session_metrics_dual.sql"
    down = MIGRATIONS_DIR / "087_session_metrics_dual_down.sql"
    assert down.exists(), "Migration 087 down file missing"
    _apply_sql(conn, up)

    # Sanity pre-check
    assert "last_context_pct_real" in _column_names(conn, "sessions_meta")

    _apply_sql(conn, down)

    cols = _column_names(conn, "sessions_meta")
    for dropped in (
        "last_context_pct_real",
        "last_context_pct_scaled",
        "last_cost_conversation_usd",
        "last_cost_session_usd",
        "last_cost_session_incomplete",
        "last_input_tokens",
        "last_output_tokens",
        "last_reasoning_tokens",
        "working_seconds_msg",
        "metrics_refreshed_at",
        "pricing_version",
    ):
        assert dropped not in cols, f"column {dropped!r} still present after down"

    cursor = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='session_conversations'"
    )
    assert cursor.fetchone() is None, "session_conversations still present after down"
