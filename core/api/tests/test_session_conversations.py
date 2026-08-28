"""PR2: session_conversations resume chain tracking + cost_session aggregation."""
from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import aiosqlite
import pytest

from core.api.services import model_registry, opencode_metrics
from core.api.services.session_metrics_service import (
    compute_cost_session,
    compute_cost_session_extended,
    on_conversation_id_changed,
)


def _seed_sessions_meta_schema(conn: sqlite3.Connection) -> None:
    """Minimal schema mirroring migration 001 + 087 for session_conversations tests."""
    conn.executescript(
        """
        CREATE TABLE sessions_meta (
            name TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL DEFAULT 'ws_default'
        );
        CREATE TABLE session_conversations (
            workspace_id TEXT NOT NULL DEFAULT 'ws_default',
            session_name TEXT NOT NULL,
            conversation_id TEXT NOT NULL,
            ord INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (workspace_id, session_name, conversation_id),
            FOREIGN KEY (session_name) REFERENCES sessions_meta(name) ON DELETE CASCADE
        );
        CREATE INDEX idx_session_conv_name ON session_conversations(session_name);
        CREATE INDEX idx_session_conv_id ON session_conversations(conversation_id);
        """
    )


def _enable_test_opencode_pricing(monkeypatch) -> None:
    """Inject fixture rates without coupling OSS tests to the excluded kb/ tree."""
    monkeypatch.setattr(
        model_registry,
        "_OPENCODE_PRICING_CACHE",
        {
            "version": "2026-04-23",
            "providers": {
                "openai": {
                    "gpt-5.4": {
                        "input": 1.25,
                        "output": 10.0,
                        "cache_read": 0.13,
                        "cache_write_5m": 1.56,
                        "cache_write_1h": 2.5,
                    }
                },
                "anthropic": {
                    "claude-sonnet-4-5": {
                        "input": 3.0,
                        "output": 15.0,
                        "cache_read": 0.3,
                        "cache_write_5m": 3.75,
                        "cache_write_1h": 6.0,
                    }
                },
            },
        },
    )


@pytest.fixture
def aiosqlite_db(tmp_path):
    """Return an aiosqlite connection to a DB seeded with the minimal schema."""
    db_path = tmp_path / "test.db"
    sync = sqlite3.connect(db_path)
    _seed_sessions_meta_schema(sync)
    sync.execute("INSERT INTO sessions_meta (name) VALUES ('demo')")
    sync.commit()
    sync.close()

    async def _open():
        conn = await aiosqlite.connect(db_path)
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA foreign_keys=ON")
        return conn

    conn = asyncio.get_event_loop().run_until_complete(_open())
    yield conn
    asyncio.get_event_loop().run_until_complete(conn.close())


@pytest.mark.asyncio
async def test_on_conversation_id_changed_appends_monotonic_ord(tmp_path):
    """Each new conv_id gets ord = prev_max + 1 starting from 0."""
    db_path = tmp_path / "test.db"
    sync = sqlite3.connect(db_path)
    _seed_sessions_meta_schema(sync)
    sync.execute("INSERT INTO sessions_meta (name) VALUES ('demo')")
    sync.commit()
    sync.close()

    conn = await aiosqlite.connect(db_path)
    conn.row_factory = aiosqlite.Row
    try:
        await on_conversation_id_changed(conn, "demo", "ses_a")
        await on_conversation_id_changed(conn, "demo", "ses_b")
        await on_conversation_id_changed(conn, "demo", "ses_c")
        await conn.commit()

        cursor = await conn.execute(
            "SELECT conversation_id, ord FROM session_conversations "
            "WHERE session_name=? ORDER BY ord",
            ("demo",),
        )
        rows = await cursor.fetchall()
        assert [(r["conversation_id"], r["ord"]) for r in rows] == [
            ("ses_a", 0),
            ("ses_b", 1),
            ("ses_c", 2),
        ]
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_on_conversation_id_changed_dedupes(tmp_path):
    """Re-appending the same conv_id is a no-op (PK composite + INSERT OR IGNORE)."""
    db_path = tmp_path / "test.db"
    sync = sqlite3.connect(db_path)
    _seed_sessions_meta_schema(sync)
    sync.execute("INSERT INTO sessions_meta (name) VALUES ('demo')")
    sync.commit()
    sync.close()

    conn = await aiosqlite.connect(db_path)
    conn.row_factory = aiosqlite.Row
    try:
        await on_conversation_id_changed(conn, "demo", "ses_a")
        await on_conversation_id_changed(conn, "demo", "ses_a")
        await on_conversation_id_changed(conn, "demo", "ses_a")
        await conn.commit()

        cursor = await conn.execute(
            "SELECT COUNT(*) as c FROM session_conversations WHERE session_name=?",
            ("demo",),
        )
        row = await cursor.fetchone()
        assert row["c"] == 1
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_on_conversation_id_changed_empty_inputs_noop(tmp_path):
    db_path = tmp_path / "test.db"
    sync = sqlite3.connect(db_path)
    _seed_sessions_meta_schema(sync)
    sync.execute("INSERT INTO sessions_meta (name) VALUES ('demo')")
    sync.commit()
    sync.close()

    conn = await aiosqlite.connect(db_path)
    conn.row_factory = aiosqlite.Row
    try:
        # Empty strings / None must not insert
        await on_conversation_id_changed(conn, "", "ses_a")
        await on_conversation_id_changed(conn, "demo", "")
        cursor = await conn.execute("SELECT COUNT(*) as c FROM session_conversations")
        row = await cursor.fetchone()
        assert row["c"] == 0
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_compute_cost_session_aggregates_two_opencode_sessions(
    tmp_path, monkeypatch
):
    """cost_session = sum of cost_conversation across chain, is_complete=True."""
    # Seed OpenCode DB with 2 sessions, each with one assistant cost>0
    oc_db = tmp_path / "opencode.db"
    sync = sqlite3.connect(oc_db)
    sync.executescript(
        """
        CREATE TABLE session (id TEXT PRIMARY KEY);
        CREATE TABLE message (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            time_created INTEGER NOT NULL,
            time_updated INTEGER NOT NULL,
            data TEXT NOT NULL
        );
        """
    )
    for sid, cost in [("ses_alpha1", 0.25), ("ses_beta22", 0.75)]:
        sync.execute("INSERT INTO session (id) VALUES (?)", (sid,))
        sync.execute(
            "INSERT INTO message (id, session_id, time_created, time_updated, data) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                f"m_{sid}",
                sid,
                1_000_000,
                1_000_000,
                json.dumps(
                    {
                        "role": "assistant",
                        "cost": cost,
                        "modelID": "claude-sonnet-4-5",
                        "finish": "stop",
                        "tokens": {
                            "total": 1000,
                            "input": 500,
                            "output": 500,
                            "reasoning": 0,
                            "cache": {"read": 0, "write": 0},
                        },
                    }
                ),
            ),
        )
    sync.commit()
    sync.close()
    monkeypatch.setattr(opencode_metrics, "OPENCODE_DB_PATH", oc_db)

    # Seed Marvis DB with session_conversations pointing at both
    marvis_db = tmp_path / "marvis.db"
    sync = sqlite3.connect(marvis_db)
    _seed_sessions_meta_schema(sync)
    sync.execute("INSERT INTO sessions_meta (name) VALUES ('oc-shell')")
    for ord_idx, sid in enumerate(["ses_alpha1", "ses_beta22"]):
        sync.execute(
            "INSERT INTO session_conversations (session_name, conversation_id, ord, created_at) "
            "VALUES (?, ?, ?, datetime('now'))",
            ("oc-shell", sid, ord_idx),
        )
    sync.commit()
    sync.close()

    conn = await aiosqlite.connect(marvis_db)
    conn.row_factory = aiosqlite.Row
    try:
        total, is_complete = await compute_cost_session(conn, "oc-shell", "opencode")
        assert is_complete is True
        assert total == pytest.approx(1.0, abs=0.01)  # 0.25 + 0.75
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_compute_cost_session_marks_incomplete_on_missing_jsonl(
    tmp_path, monkeypatch
):
    """is_complete=False when the provider returns None for any conv_id."""
    oc_db = tmp_path / "opencode.db"
    sync = sqlite3.connect(oc_db)
    sync.executescript(
        """
        CREATE TABLE session (id TEXT PRIMARY KEY);
        CREATE TABLE message (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            time_created INTEGER NOT NULL,
            time_updated INTEGER NOT NULL,
            data TEXT NOT NULL
        );
        """
    )
    # Only ses_present has data; ses_missing is in the chain but has no messages
    sync.execute("INSERT INTO session (id) VALUES ('ses_present1')")
    sync.execute(
        "INSERT INTO message (id, session_id, time_created, time_updated, data) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            "m1",
            "ses_present1",
            1_000_000,
            1_000_000,
            json.dumps(
                {
                    "role": "assistant",
                    "cost": 0.5,
                    "modelID": "claude-sonnet-4-5",
                    "finish": "stop",
                    "tokens": {
                        "total": 1000,
                        "input": 500,
                        "output": 500,
                        "reasoning": 0,
                        "cache": {"read": 0, "write": 0},
                    },
                }
            ),
        ),
    )
    sync.commit()
    sync.close()
    monkeypatch.setattr(opencode_metrics, "OPENCODE_DB_PATH", oc_db)

    marvis_db = tmp_path / "marvis.db"
    sync = sqlite3.connect(marvis_db)
    _seed_sessions_meta_schema(sync)
    sync.execute("INSERT INTO sessions_meta (name) VALUES ('oc-shell')")
    # ses_missing is NOT in opencode.db — parse_session returns None
    for ord_idx, sid in enumerate(["ses_present1", "ses_missing1"]):
        sync.execute(
            "INSERT INTO session_conversations (session_name, conversation_id, ord, created_at) "
            "VALUES (?, ?, ?, datetime('now'))",
            ("oc-shell", sid, ord_idx),
        )
    sync.commit()
    sync.close()

    conn = await aiosqlite.connect(marvis_db)
    conn.row_factory = aiosqlite.Row
    try:
        total, is_complete = await compute_cost_session(conn, "oc-shell", "opencode")
        assert is_complete is False
        assert total == pytest.approx(0.5, abs=0.01)
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_compute_cost_session_empty_chain_returns_complete_zero(tmp_path):
    """Session without tracked conversations → (0.0, True) — nothing missing."""
    marvis_db = tmp_path / "marvis.db"
    sync = sqlite3.connect(marvis_db)
    _seed_sessions_meta_schema(sync)
    sync.execute("INSERT INTO sessions_meta (name) VALUES ('never-tracked')")
    sync.commit()
    sync.close()

    conn = await aiosqlite.connect(marvis_db)
    conn.row_factory = aiosqlite.Row
    try:
        total, is_complete = await compute_cost_session(conn, "never-tracked", "opencode")
        assert total == 0.0
        assert is_complete is True
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_compute_cost_session_unknown_provider_returns_incomplete(tmp_path):
    marvis_db = tmp_path / "marvis.db"
    sync = sqlite3.connect(marvis_db)
    _seed_sessions_meta_schema(sync)
    sync.execute("INSERT INTO sessions_meta (name) VALUES ('weird')")
    sync.execute(
        "INSERT INTO session_conversations (session_name, conversation_id, ord, created_at) "
        "VALUES (?, ?, ?, datetime('now'))",
        ("weird", "ses_x", 0),
    )
    sync.commit()
    sync.close()

    conn = await aiosqlite.connect(marvis_db)
    conn.row_factory = aiosqlite.Row
    try:
        total, is_complete = await compute_cost_session(conn, "weird", "does-not-exist")
        assert total == 0.0
        assert is_complete is True  # empty-provider path short-circuits
    finally:
        await conn.close()


# --------------------------------------------------------------------------
# PR4 — compute_cost_session_extended: real + equivalent (shadow) aggregation
# --------------------------------------------------------------------------


def _seed_opencode_session_with_pricing(
    db_path, session_id, cost, provider_id, model_id, input_tokens, output_tokens
):
    """Append a session to an existing opencode DB file (creates tables if missing)."""
    sync = sqlite3.connect(db_path)
    sync.executescript(
        """
        CREATE TABLE IF NOT EXISTS session (id TEXT PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS message (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            time_created INTEGER NOT NULL,
            time_updated INTEGER NOT NULL,
            data TEXT NOT NULL
        );
        """
    )
    sync.execute("INSERT OR IGNORE INTO session (id) VALUES (?)", (session_id,))
    sync.execute(
        "INSERT INTO message (id, session_id, time_created, time_updated, data) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            f"m_{session_id}",
            session_id,
            1_000_000,
            1_000_000,
            json.dumps(
                {
                    "role": "assistant",
                    "cost": cost,
                    "providerID": provider_id,
                    "modelID": model_id,
                    "finish": "stop",
                    "tokens": {
                        "total": input_tokens + output_tokens,
                        "input": input_tokens,
                        "output": output_tokens,
                        "reasoning": 0,
                        "cache": {"read": 0, "write": 0},
                    },
                }
            ),
        ),
    )
    sync.commit()
    sync.close()


@pytest.mark.asyncio
async def test_compute_cost_session_extended_aggregates_equivalent(
    tmp_path, monkeypatch
):
    """cost_session_extended sums real + equivalent across chain."""
    _enable_test_opencode_pricing(monkeypatch)
    oc_db = tmp_path / "opencode.db"
    # Session 1: OAuth-style — real cost=0, equivalent from tokens
    #   gpt-5.4 pricing: input=$1.25/M, output=$10.00/M → (100000*1.25 + 1000*10)/1M = 0.135
    _seed_opencode_session_with_pricing(
        oc_db, "ses_free001", 0.0, "openai", "gpt-5.4", 100_000, 1000
    )
    # Session 2: already-paid — real cost=0.50, equivalent = same formula
    #   sonnet-4-5: input=$3.00/M, output=$15.00/M → (10000*3 + 1000*15)/1M = 0.045
    _seed_opencode_session_with_pricing(
        oc_db, "ses_paid002", 0.50, "anthropic", "claude-sonnet-4-5", 10_000, 1000
    )
    monkeypatch.setattr(opencode_metrics, "OPENCODE_DB_PATH", oc_db)

    marvis_db = tmp_path / "marvis.db"
    sync = sqlite3.connect(marvis_db)
    _seed_sessions_meta_schema(sync)
    sync.execute("INSERT INTO sessions_meta (name) VALUES ('mixed')")
    for ord_idx, sid in enumerate(["ses_free001", "ses_paid002"]):
        sync.execute(
            "INSERT INTO session_conversations (session_name, conversation_id, ord, created_at) "
            "VALUES (?, ?, ?, datetime('now'))",
            ("mixed", sid, ord_idx),
        )
    sync.commit()
    sync.close()

    conn = await aiosqlite.connect(marvis_db)
    conn.row_factory = aiosqlite.Row
    try:
        (
            total_real,
            total_equivalent,
            version,
            is_complete,
        ) = await compute_cost_session_extended(conn, "mixed", "opencode")
        assert is_complete is True
        assert total_real == pytest.approx(0.50, abs=0.01)
        # equivalent = 0.135 + 0.045 = 0.18
        assert total_equivalent is not None
        assert total_equivalent == pytest.approx(0.18, abs=0.01)
        assert version == "2026-04-23"
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_compute_cost_session_extended_equivalent_none_when_no_pricing(
    tmp_path, monkeypatch
):
    """When no message in chain has known pricing, equivalent must be None."""
    oc_db = tmp_path / "opencode.db"
    # Unknown provider — skip, no equivalent computed
    _seed_opencode_session_with_pricing(
        oc_db, "ses_unknown1", 0.10, "mystery", "mystery-xl", 1000, 500
    )
    monkeypatch.setattr(opencode_metrics, "OPENCODE_DB_PATH", oc_db)

    marvis_db = tmp_path / "marvis.db"
    sync = sqlite3.connect(marvis_db)
    _seed_sessions_meta_schema(sync)
    sync.execute("INSERT INTO sessions_meta (name) VALUES ('unknown-only')")
    sync.execute(
        "INSERT INTO session_conversations (session_name, conversation_id, ord, created_at) "
        "VALUES (?, ?, ?, datetime('now'))",
        ("unknown-only", "ses_unknown1", 0),
    )
    sync.commit()
    sync.close()

    conn = await aiosqlite.connect(marvis_db)
    conn.row_factory = aiosqlite.Row
    try:
        (
            total_real,
            total_equivalent,
            version,
            is_complete,
        ) = await compute_cost_session_extended(conn, "unknown-only", "opencode")
        assert is_complete is True
        assert total_real == pytest.approx(0.10, abs=0.01)
        # No known pricing in chain → equivalent stays None (fallback=skip)
        assert total_equivalent is None
        assert version is None
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_compute_cost_session_backward_compat_tuple(tmp_path, monkeypatch):
    """Legacy 2-tuple compute_cost_session still works (PR2 callers)."""
    oc_db = tmp_path / "opencode.db"
    _seed_opencode_session_with_pricing(
        oc_db, "ses_compat01", 0.25, "anthropic", "claude-sonnet-4-5", 1000, 500
    )
    monkeypatch.setattr(opencode_metrics, "OPENCODE_DB_PATH", oc_db)

    marvis_db = tmp_path / "marvis.db"
    sync = sqlite3.connect(marvis_db)
    _seed_sessions_meta_schema(sync)
    sync.execute("INSERT INTO sessions_meta (name) VALUES ('compat')")
    sync.execute(
        "INSERT INTO session_conversations (session_name, conversation_id, ord, created_at) "
        "VALUES (?, ?, ?, datetime('now'))",
        ("compat", "ses_compat01", 0),
    )
    sync.commit()
    sync.close()

    conn = await aiosqlite.connect(marvis_db)
    conn.row_factory = aiosqlite.Row
    try:
        result = await compute_cost_session(conn, "compat", "opencode")
        assert isinstance(result, tuple)
        assert len(result) == 2
        total, is_complete = result
        assert total == pytest.approx(0.25, abs=0.01)
        assert is_complete is True
    finally:
        await conn.close()
