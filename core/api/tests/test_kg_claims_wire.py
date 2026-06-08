"""Fase 2 — claims wired additively into graph_impact (covers project_impact too,
which delegates to the same use-case).

flag-off → no `claims` key (byte-identical); flag-on → additive `claims` with the
existing fields untouched; a claims-build error is fail-soft (impact still returns).
The heavy deps (BFS, git freshness, node existence) are stubbed; _build_claims runs
for real against an in-memory graph.
"""
from __future__ import annotations

import aiosqlite
import pytest

import core.api.use_cases.graph as graph_mod
from core.api.config import settings
from core.api.services import graph_service
from core.api.use_cases import graph as uc
from core.api.use_cases._context import CallerContext

CTX = CallerContext.local_single_user()
SUBJECT = "py:function:hub"

_SCHEMA = """
CREATE TABLE graph_nodes (
    id TEXT PRIMARY KEY, type TEXT, last_seen_at TEXT, last_verified_at TEXT
);
CREATE TABLE graph_edges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id TEXT NOT NULL, target_id TEXT NOT NULL, relation TEXT NOT NULL,
    valid_until TEXT, first_seen_at TEXT
);
"""

_IMPACT_STUB = {
    "target": SUBJECT,
    "direct_callers": [{"node_id": "py:function:caller"}],
    "transitive_list": [],
    "rank_score": 1,
}


async def _stub_exists(db, node_id, **kw):
    return True


async def _stub_impact(db, **kw):
    # return a copy so the use-case mutating `result` doesn't bleed across tests
    return dict(_IMPACT_STUB)


async def _stub_fresh(db, **kw):
    return {"stale": False}


async def _db():
    conn = await aiosqlite.connect(":memory:")
    await conn.executescript(_SCHEMA)
    await conn.execute(
        "INSERT INTO graph_nodes (id,type,last_seen_at,last_verified_at) "
        "VALUES (?,?,?,?)",
        (SUBJECT, "function", "2026-06-07T00:00:00Z", None),
    )
    await conn.executemany(
        "INSERT INTO graph_edges (source_id,target_id,relation,valid_until,first_seen_at) "
        "VALUES (?,?,?,?,?)",
        [
            ("a", SUBJECT, "depends_on", None, "x"),
            ("b", SUBJECT, "depends_on", None, "x"),
            ("m", SUBJECT, "refers_to", None, "x"),
        ],
    )
    await conn.commit()
    return conn


def _patch_heavy(monkeypatch):
    monkeypatch.setattr(graph_service, "node_exists_at", _stub_exists)
    monkeypatch.setattr(graph_service, "graph_impact", _stub_impact)
    monkeypatch.setattr(graph_mod, "compute_index_freshness", _stub_fresh)


@pytest.mark.asyncio
async def test_flag_off_no_claims_key(monkeypatch):
    monkeypatch.setattr(settings, "kg_claims_enabled", False)
    _patch_heavy(monkeypatch)
    db = await _db()
    try:
        res = await uc.graph_impact(CTX, db, node_id=SUBJECT)
        assert "claims" not in res  # byte-identical: nothing added
        assert res["direct_callers"] == [{"node_id": "py:function:caller"}]
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_flag_on_adds_claims_additively(monkeypatch):
    monkeypatch.setattr(settings, "kg_claims_enabled", True)
    _patch_heavy(monkeypatch)
    db = await _db()
    try:
        res = await uc.graph_impact(CTX, db, node_id=SUBJECT)
        assert "claims" in res
        by_kind = {c["kind"]: c for c in res["claims"]}
        assert by_kind["dependents_depends_on"]["value"] == 2  # real depends_on count
        assert by_kind["mentioned_by"]["value"] == 1            # refers_to, NOT a dependency
        # existing fields untouched (additive)
        assert res["direct_callers"] == [{"node_id": "py:function:caller"}]
        assert res["rank_score"] == 1
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_flag_on_claims_fail_soft(monkeypatch):
    monkeypatch.setattr(settings, "kg_claims_enabled", True)
    _patch_heavy(monkeypatch)

    async def _boom(*a, **k):
        raise RuntimeError("claims build blew up")

    monkeypatch.setattr(graph_mod, "_build_claims", _boom)
    db = await _db()
    try:
        res = await uc.graph_impact(CTX, db, node_id=SUBJECT)
        assert "claims" not in res        # omitted, not crashed
        assert res["direct_callers"]       # impact response intact
    finally:
        await db.close()
