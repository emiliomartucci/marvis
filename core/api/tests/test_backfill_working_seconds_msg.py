"""PR3: backfill script for working_seconds_msg (pre-PR3 gate)."""
from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

from core.api.services.claude_metrics import SessionMetrics

SCRIPT = (
    Path(__file__).resolve().parent.parent.parent
    / "scripts"
    / "backfill_working_seconds_msg.py"
)


def _load_module():
    spec = importlib.util.spec_from_file_location("backfill_ws", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _seed(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE sessions_meta (
            name TEXT PRIMARY KEY,
            conversation_id TEXT,
            provider TEXT DEFAULT 'claude',
            hibernated INTEGER DEFAULT 0,
            working_seconds_msg INTEGER
        );
        """
    )
    conn.executemany(
        "INSERT INTO sessions_meta "
        "(name, conversation_id, provider, hibernated, working_seconds_msg) "
        "VALUES (?, ?, ?, ?, ?)",
        [
            # Needs backfill: active + has conv_id + NULL wsmsg
            ("sess-active", "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", "claude", 0, None),
            # Skipped: hibernated
            ("sess-hib", "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb", "claude", 1, None),
            # Skipped: already populated
            ("sess-done", "cccccccc-cccc-cccc-cccc-cccccccccccc", "claude", 0, 42),
            # Skipped: no conv_id
            ("sess-noconv", None, "claude", 0, None),
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


def _fake_metrics(**overrides) -> SessionMetrics:
    defaults = dict(
        conversation_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        model="claude-opus-4-5",
        context_pct=12.0,
        cost_usd=0.5,
        message_count=10,
        duration_minutes=5.0,
        working_seconds_msg=123,
    )
    defaults.update(overrides)
    return SessionMetrics(**defaults)


def test_backfill_updates_only_active_null_rows(seeded_db):
    module = _load_module()

    class FakeProvider:
        def parse_session(self, conv_id, cwd=None):
            return _fake_metrics()

    with patch.object(module, "get_metrics_provider", return_value=FakeProvider()):
        updated, total = module.backfill(seeded_db)

    assert total == 1  # only sess-active matches SELECT filter
    assert updated == 1

    conn = sqlite3.connect(seeded_db)
    try:
        rows = dict(
            conn.execute(
                "SELECT name, working_seconds_msg FROM sessions_meta"
            ).fetchall()
        )
    finally:
        conn.close()
    assert rows["sess-active"] == 123
    assert rows["sess-hib"] is None  # untouched (hibernated filter)
    assert rows["sess-done"] == 42  # untouched (already set)
    assert rows["sess-noconv"] is None


def test_backfill_handles_parser_exceptions(seeded_db, capsys):
    module = _load_module()

    class BrokenProvider:
        def parse_session(self, conv_id, cwd=None):
            raise RuntimeError("boom")

    with patch.object(module, "get_metrics_provider", return_value=BrokenProvider()):
        updated, total = module.backfill(seeded_db)

    assert total == 1
    assert updated == 0
    assert "boom" in capsys.readouterr().out


def test_backfill_skips_unknown_provider(seeded_db):
    module = _load_module()
    with patch.object(module, "get_metrics_provider", return_value=None):
        updated, total = module.backfill(seeded_db)
    assert total == 1
    assert updated == 0
