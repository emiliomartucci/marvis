"""Migration 149: KG trust columns (superseded_by on edges, last_verified_at on nodes).

Additive ADD COLUMN migration (Fase C). Mirrors the 087/088/089 pattern: a minimal
precursor schema (no FTS/vec0 extensions) seeded at v148, applied via executescript
exactly like the runtime runner.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent.parent.parent / "migrations"


def _seed_minimum_schema(conn: sqlite3.Connection) -> None:
    """Minimal graph_edges + graph_nodes (mig-067 temporal cols present) at v148."""
    conn.executescript(
        """
        CREATE TABLE schema_versions (version INTEGER PRIMARY KEY);
        CREATE TABLE graph_edges (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id TEXT NOT NULL,
            target_id TEXT NOT NULL,
            relation TEXT NOT NULL,
            first_seen_at TEXT,
            last_seen_at TEXT,
            valid_until TEXT,
            UNIQUE(source_id, target_id, relation)
        );
        CREATE TABLE graph_nodes (
            id TEXT PRIMARY KEY,
            type TEXT NOT NULL,
            deprecated_at TEXT,
            last_seen_at TEXT
        );
        INSERT INTO schema_versions (version) VALUES (148);
        INSERT INTO graph_nodes (id, type) VALUES ('py:function:a', 'function');
        INSERT INTO graph_edges (source_id, target_id, relation)
            VALUES ('py:function:a', 'py:function:a', 'calls');
        """
    )
    conn.commit()


def _apply_sql(conn: sqlite3.Connection, path: Path) -> None:
    conn.executescript(path.read_text())
    conn.commit()


def _column_info(conn: sqlite3.Connection, table: str) -> dict[str, sqlite3.Row]:
    cursor = conn.execute(f"PRAGMA table_info({table})")
    return {row["name"]: row for row in cursor.fetchall()}


@pytest.fixture
def fresh_db(tmp_path):
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    _seed_minimum_schema(conn)
    yield conn, db_path
    conn.close()


def test_up_adds_trust_columns_nullable(fresh_db):
    conn, _ = fresh_db
    up = MIGRATIONS_DIR / "149_kg_trust_columns.sql"
    assert up.exists(), "Migration 149 up file missing"
    _apply_sql(conn, up)

    edges = _column_info(conn, "graph_edges")
    nodes = _column_info(conn, "graph_nodes")
    assert "superseded_by" in edges
    assert "last_verified_at" in nodes
    # nullable, no default expression (constant-time ADD COLUMN, safe on huge tables)
    assert edges["superseded_by"]["notnull"] == 0
    assert edges["superseded_by"]["dflt_value"] is None
    assert nodes["last_verified_at"]["notnull"] == 0
    assert nodes["last_verified_at"]["dflt_value"] is None


def test_up_existing_rows_get_null(fresh_db):
    conn, _ = fresh_db
    _apply_sql(conn, MIGRATIONS_DIR / "149_kg_trust_columns.sql")
    assert conn.execute("SELECT superseded_by FROM graph_edges").fetchone()[0] is None
    assert conn.execute("SELECT last_verified_at FROM graph_nodes").fetchone()[0] is None


def test_up_bumps_schema_version(fresh_db):
    conn, _ = fresh_db
    _apply_sql(conn, MIGRATIONS_DIR / "149_kg_trust_columns.sql")
    versions = {r[0] for r in conn.execute("SELECT version FROM schema_versions")}
    assert versions == {148, 149}


def test_up_preserves_unique_triple(fresh_db):
    """UNIQUE(source,target,relation) is NOT rebuilt — soft supersession is
    in-place via valid_until, so the constraint must still reject a dup triple."""
    conn, _ = fresh_db
    _apply_sql(conn, MIGRATIONS_DIR / "149_kg_trust_columns.sql")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO graph_edges (source_id, target_id, relation) "
            "VALUES ('py:function:a', 'py:function:a', 'calls')"
        )


def test_down_is_non_destructive(fresh_db):
    """Down removes only the version marker; the nullable columns stay (zero-cost),
    matching mig 067's choice for these FTS-triggered tables."""
    conn, _ = fresh_db
    _apply_sql(conn, MIGRATIONS_DIR / "149_kg_trust_columns.sql")
    _apply_sql(conn, MIGRATIONS_DIR / "149_kg_trust_columns_down.sql")
    versions = {r[0] for r in conn.execute("SELECT version FROM schema_versions")}
    assert 149 not in versions
    assert versions == {148}
    # columns deliberately remain
    assert "superseded_by" in _column_info(conn, "graph_edges")
    assert "last_verified_at" in _column_info(conn, "graph_nodes")
