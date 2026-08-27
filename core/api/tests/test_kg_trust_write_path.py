"""Fase D — KG trust write-path (mark_node_verified / mark_edge_superseded).

The apply actions an agent performs after Triage approves a Brain proposal.
Flag-gated by MARVIS_TEMPORAL_MEMORY: inert (ValidationError) when off.
"""
from __future__ import annotations

import aiosqlite
import pytest

from core.api.config import settings
from core.api.use_cases import graph as uc
from core.api.use_cases._context import CallerContext
from core.api.use_cases._errors import NotFoundError, ValidationError

_SCHEMA = """
CREATE TABLE graph_nodes (
    id TEXT PRIMARY KEY,
    type TEXT,
    project_id TEXT,
    deprecated_at TEXT,
    last_verified_at TEXT
);
CREATE TABLE graph_edges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id TEXT NOT NULL,
    target_id TEXT NOT NULL,
    relation TEXT NOT NULL,
    valid_until TEXT,
    superseded_by TEXT,
    UNIQUE(source_id, target_id, relation)
);
"""

CTX = CallerContext.local_single_user()


async def _db():
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await conn.executescript(_SCHEMA)
    await conn.executemany(
        "INSERT INTO graph_nodes (id, type, project_id) "
        "VALUES (?, 'function', 'marvisx')",
        [("py:function:a",), ("py:function:b",)],
    )
    await conn.execute(
        "INSERT INTO graph_edges (source_id, target_id, relation) "
        "VALUES ('py:function:a', 'py:function:b', 'depends_on')"
    )
    await conn.commit()
    return conn


@pytest.mark.asyncio
async def test_node_verify_flag_off_is_inert(monkeypatch):
    monkeypatch.setattr(settings, "temporal_memory_enabled", False)
    db = await _db()
    try:
        with pytest.raises(ValidationError):
            await uc.mark_node_verified(CTX, db, node_id="py:function:a")
        cur = await db.execute(
            "SELECT last_verified_at FROM graph_nodes WHERE id = 'py:function:a'"
        )
        assert (await cur.fetchone())[0] is None  # nothing written
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_node_verify_sets_timestamp(monkeypatch):
    monkeypatch.setattr(settings, "temporal_memory_enabled", True)
    db = await _db()
    try:
        out = await uc.mark_node_verified(CTX, db, node_id="py:function:a")
        assert out["action"] == "verified"
        assert out["last_verified_at"]
        cur = await db.execute(
            "SELECT last_verified_at FROM graph_nodes WHERE id = 'py:function:a'"
        )
        assert (await cur.fetchone())[0] == out["last_verified_at"]
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_node_verify_missing_node_404(monkeypatch):
    monkeypatch.setattr(settings, "temporal_memory_enabled", True)
    db = await _db()
    try:
        with pytest.raises(NotFoundError):
            await uc.mark_node_verified(CTX, db, node_id="py:function:zzz")
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_edge_supersede_sets_valid_until_and_pointer(monkeypatch):
    monkeypatch.setattr(settings, "temporal_memory_enabled", True)
    db = await _db()
    try:
        out = await uc.mark_edge_superseded(
            CTX, db,
            source_id="py:function:a", target_id="py:function:b",
            relation="depends_on", superseded_by="42",
        )
        assert out["action"] == "superseded"
        cur = await db.execute(
            "SELECT valid_until, superseded_by FROM graph_edges "
            "WHERE source_id = 'py:function:a'"
        )
        row = await cur.fetchone()
        assert row[0] == out["valid_until"]
        assert row[1] == "42"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_edge_supersede_invalid_relation(monkeypatch):
    monkeypatch.setattr(settings, "temporal_memory_enabled", True)
    db = await _db()
    try:
        with pytest.raises(ValidationError):
            await uc.mark_edge_superseded(
                CTX, db,
                source_id="py:function:a", target_id="py:function:b",
                relation="not_a_relation",
            )
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_edge_supersede_missing_triple_404(monkeypatch):
    monkeypatch.setattr(settings, "temporal_memory_enabled", True)
    db = await _db()
    try:
        with pytest.raises(NotFoundError):
            await uc.mark_edge_superseded(
                CTX, db,
                source_id="py:function:a", target_id="py:function:zzz",
                relation="depends_on",
            )
    finally:
        await db.close()
