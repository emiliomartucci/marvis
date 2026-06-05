"""PR3: backfill script for session_conversations table."""
from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path

import pytest

SCRIPT = (
    Path(__file__).resolve().parent.parent.parent
    / "scripts"
    / "backfill_session_conversations.py"
)


def _load_module():
    spec = importlib.util.spec_from_file_location("backfill_sc", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _seed(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE sessions_meta (
            name TEXT PRIMARY KEY,
            conversation_id TEXT
        );
        CREATE TABLE session_conversations (
            session_name TEXT NOT NULL,
            conversation_id TEXT NOT NULL,
            ord INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (session_name, conversation_id),
            FOREIGN KEY (session_name) REFERENCES sessions_meta(name) ON DELETE CASCADE
        );
        """
    )
    conn.executemany(
        "INSERT INTO sessions_meta (name, conversation_id) VALUES (?, ?)",
        [
            ("sess-a", "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"),
            ("sess-b", "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"),
            ("sess-c", None),  # no conv_id, should be skipped
        ],
    )
    conn.commit()


@pytest.fixture
def seeded_db(tmp_path):
    db_path = tmp_path / "console.db"
    conn = sqlite3.connect(db_path)
    _seed(conn)
    conn.close()
    return db_path


def test_backfill_inserts_rows(seeded_db):
    module = _load_module()
    inserted, total = module.backfill(seeded_db)
    assert total == 2  # only sess-a and sess-b have conv_id
    assert inserted == 2

    conn = sqlite3.connect(seeded_db)
    try:
        rows = conn.execute(
            "SELECT session_name, conversation_id, ord FROM session_conversations "
            "ORDER BY session_name"
        ).fetchall()
    finally:
        conn.close()
    assert rows == [
        ("sess-a", "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", 0),
        ("sess-b", "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb", 0),
    ]


def test_backfill_idempotent(seeded_db):
    module = _load_module()
    first_inserted, _ = module.backfill(seeded_db)
    assert first_inserted == 2

    # Re-run: INSERT OR IGNORE must be a no-op
    second_inserted, second_total = module.backfill(seeded_db)
    assert second_inserted == 0
    assert second_total == 2
