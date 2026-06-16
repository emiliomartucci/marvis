"""Unit tests for the KG freshness/staleness signal (#13).

graph_impact / graph_neighbors must not return an authoritative-looking blast
radius with no hint that the index is stale or partial. compute_index_freshness
surfaces, per the queried node's project, whether the graph is indexed / has git
provenance / matches HEAD — and NEVER mutates (no reindex from a read).
"""

from __future__ import annotations

import aiosqlite
import pytest

import core.api.use_cases.graph as graph_mod
from core.api.use_cases.graph import compute_index_freshness

_SCHEMA = """
CREATE TABLE graph_nodes (
    id TEXT PRIMARY KEY,
    project_id TEXT,
    last_modified_git_sha TEXT,
    touch_last_at TEXT
);
"""


async def _db(rows):
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await conn.executescript(_SCHEMA)
    for r in rows:
        await conn.execute(
            "INSERT INTO graph_nodes (id, project_id, last_modified_git_sha, touch_last_at) "
            "VALUES (?, ?, ?, ?)",
            r,
        )
    await conn.commit()
    return conn


@pytest.mark.asyncio
async def test_not_indexed_is_stale():
    db = await _db([])
    try:
        out = await compute_index_freshness(db, node_id="py:function:x.y", project="alpha")
        assert out["reason"] == "not_indexed"
        assert out["stale"] is True
        assert "index" in out["hint"]
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_not_git_linked_is_not_stale(monkeypatch):
    monkeypatch.setattr("core.api.routers.projects._find_git_path", lambda slug: None)
    db = await _db([("py:function:x.y", "alpha", "abc1234", "2026-05-01")])
    try:
        out = await compute_index_freshness(db, node_id="py:function:x.y", project="alpha")
        assert out["reason"] == "not_git_linked"
        assert out["stale"] is False
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_sha_unpopulated_is_stale(monkeypatch):
    # The real-world case from the issue: nodes exist + repo linked, but commit
    # provenance was never populated (distinct git_sha = 0).
    monkeypatch.setattr("core.api.routers.projects._find_git_path", lambda slug: "/tmp/repo")
    monkeypatch.setattr(graph_mod, "_git_head_sha", lambda repo: "deadbee")
    db = await _db(
        [
            ("py:function:x.y", "alpha", "", "2026-05-01"),
            ("py:file:x", "alpha", None, "2026-05-02"),
        ]
    )
    try:
        out = await compute_index_freshness(db, node_id="py:function:x.y", project="alpha")
        assert out["reason"] == "sha_unpopulated"
        assert out["stale"] is True
        assert out["indexed_node_count"] == 2
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_behind_head_is_stale(monkeypatch):
    monkeypatch.setattr("core.api.routers.projects._find_git_path", lambda slug: "/tmp/repo")
    monkeypatch.setattr(graph_mod, "_git_head_sha", lambda repo: "newsha9")
    db = await _db([("py:function:x.y", "alpha", "oldsha1", "2026-05-01")])
    try:
        out = await compute_index_freshness(db, node_id="py:function:x.y", project="alpha")
        assert out["reason"] == "behind_head"
        assert out["stale"] is True
        assert out["indexed_git_sha"] == "oldsha1"
        assert out["head_git_sha"] == "newsha9"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_fresh_when_indexed_sha_matches_head(monkeypatch):
    monkeypatch.setattr("core.api.routers.projects._find_git_path", lambda slug: "/tmp/repo")
    monkeypatch.setattr(graph_mod, "_git_head_sha", lambda repo: "abc1234")
    db = await _db([("py:function:x.y", "alpha", "abc1234", "2026-05-01")])
    try:
        out = await compute_index_freshness(db, node_id="py:function:x.y", project="alpha")
        assert out["reason"] == "fresh"
        assert out["stale"] is False
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_autoreindex_flag_marks_command_but_never_executes(monkeypatch):
    monkeypatch.setattr(
        "core.api.config.settings.graph_autoreindex_on_drift", True, raising=False
    )
    db = await _db([])  # not_indexed → stale
    try:
        out = await compute_index_freshness(db, node_id="py:function:x.y", project="alpha")
        assert out["stale"] is True
        assert out.get("auto_reindex") == "suggested"
        assert out["reindex_command"] == "marvis project index alpha"
    finally:
        await db.close()
