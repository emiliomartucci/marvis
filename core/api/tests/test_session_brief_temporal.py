"""Fase F — session_brief temporal surface (_temporal_brief).

Flag-gated by MARVIS_TEMPORAL_MEMORY: None (no-op) when off; two counts (open
Brain proposals + aging-unverified review backlog) when on; None on missing
tables (defensive — never breaks the brief).
"""
from __future__ import annotations

import aiosqlite
import pytest

from core.api.config import settings
from core.api.use_cases.projects import _temporal_brief

_SCHEMA = """
CREATE TABLE brain_memory_operations (
    operation_id TEXT PRIMARY KEY, scope_type TEXT, scope_key TEXT, approval_state TEXT
);
CREATE TABLE graph_nodes (
    id TEXT PRIMARY KEY, project_id TEXT,
    deprecated_at TEXT, last_verified_at TEXT, last_seen_at TEXT
);
"""


async def _db(with_tables: bool = True):
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    if with_tables:
        await conn.executescript(_SCHEMA)
    return conn


@pytest.mark.asyncio
async def test_flag_off_is_none(monkeypatch):
    monkeypatch.setattr(settings, "temporal_memory_enabled", False)
    db = await _db()
    try:
        assert await _temporal_brief(db, "marvisx") is None
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_counts_open_loops_and_review_backlog(monkeypatch):
    monkeypatch.setattr(settings, "temporal_memory_enabled", True)
    db = await _db()
    try:
        await db.executemany(
            "INSERT INTO brain_memory_operations VALUES (?,?,?,?)",
            [
                ("o1", "project", "marvisx", "pending"),
                ("o2", "project", "marvisx", "pending"),
                ("o3", "project", "marvisx", "approved"),  # not pending
                ("o4", "project", "other", "pending"),      # other project
            ],
        )
        await db.executemany(
            "INSERT INTO graph_nodes VALUES (?,?,?,?,?)",
            [
                ("py:function:a", "marvisx", None, None, "2026-01-01 10:00:00"),  # counted
                ("py:function:b", "marvisx", None, "2026-06-01 10:00:00", "2026-01-01 10:00:00"),  # verified
                ("py:function:c", "marvisx", "2026-05-01 10:00:00", None, "2026-01-01 10:00:00"),  # deprecated
                ("py:function:d", "marvisx", None, None, "2099-01-01 10:00:00"),  # not aging
                ("py:function:e", "other", None, None, "2026-01-01 10:00:00"),    # other project
            ],
        )
        await db.commit()
        out = await _temporal_brief(db, "marvisx")
        assert out == {"brain_open_loops": 2, "needs_review_count": 1}
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_missing_tables_is_none(monkeypatch):
    monkeypatch.setattr(settings, "temporal_memory_enabled", True)
    db = await _db(with_tables=False)
    try:
        assert await _temporal_brief(db, "marvisx") is None
    finally:
        await db.close()
