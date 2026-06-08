"""Fase 1 — answer-ready claims engine: SQL count/sample helpers + _build_claims.

The DB counts (relation-typed, live-only, self-edge-free, capped provenance); the
builder assembles KGClaim objects. No tool wiring yet (Fase 2). In-memory aiosqlite
fixture — no FTS/vec0 needed.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import aiosqlite
import pytest

from core.api.config import settings
from core.api.models.graph_ux import KGClaim
from core.api.services import graph_service
from core.api.use_cases.graph import _build_claims

NOW = datetime(2026, 6, 8, tzinfo=timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


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


async def _db(nodes=(), edges=()):
    conn = await aiosqlite.connect(":memory:")
    await conn.executescript(_SCHEMA)
    for n in nodes:
        await conn.execute(
            "INSERT INTO graph_nodes (id,type,last_seen_at,last_verified_at) VALUES (?,?,?,?)",
            n,
        )
    for e in edges:
        # (source_id, target_id, relation, valid_until, first_seen_at)
        await conn.execute(
            "INSERT INTO graph_edges (source_id,target_id,relation,valid_until,first_seen_at) "
            "VALUES (?,?,?,?,?)",
            e,
        )
    await conn.commit()
    return conn


# --- SQL helpers -----------------------------------------------------------

@pytest.mark.asyncio
async def test_count_live_only_and_distinct():
    db = await _db(edges=[
        ("a", "hub", "depends_on", None, "2026-01-01"),
        ("b", "hub", "depends_on", None, "2026-01-02"),
        ("b", "hub", "depends_on", None, "2026-01-03"),          # dup source → DISTINCT
        ("c", "hub", "depends_on", "2026-05-01T00:00:00Z", "x"),  # superseded → excluded
    ])
    try:
        n = await graph_service.count_in_edges_by_relation(db, "hub", "depends_on")
        assert n == 2  # a, b (distinct); c superseded; relation in-degree only
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_count_excludes_self_edge_and_exclude_sources():
    db = await _db(edges=[
        ("hub", "hub", "depends_on", None, "x"),  # self-edge → excluded
        ("a", "hub", "depends_on", None, "x"),
        ("meta", "hub", "depends_on", None, "x"),
    ])
    try:
        assert await graph_service.count_in_edges_by_relation(db, "hub", "depends_on") == 2
        assert await graph_service.count_in_edges_by_relation(
            db, "hub", "depends_on", exclude_sources=("meta",)
        ) == 1
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_sample_capped_in_sql():
    edges = [(f"s{i}", "hub", "refers_to", None, f"2026-01-{i:02d}") for i in range(1, 26)]
    db = await _db(edges=edges)
    try:
        srcs = await graph_service.sample_in_edge_sources(db, "hub", "refers_to", limit=10)
        assert len(srcs) == 10  # capped in SQL, not 25
        assert await graph_service.count_in_edges_by_relation(db, "hub", "refers_to") == 25
    finally:
        await db.close()


# --- _build_claims ---------------------------------------------------------

@pytest.mark.asyncio
async def test_build_claims_dependency_vs_associative():
    db = await _db(
        nodes=[("hub", "project", _iso(NOW - timedelta(days=1)), None)],
        edges=(
            [("d1", "hub", "depends_on", None, "x"), ("d2", "hub", "depends_on", None, "x")]
            + [(f"m{i}", "hub", "refers_to", None, "x") for i in range(5)]
            + [("x1", "hub", "mentions", None, "x")]
        ),
    )
    try:
        claims = await _build_claims(db, "hub", now=NOW)
        by_kind = {c["kind"]: c for c in claims}
        # real dependency claim, value = live depends_on in-degree
        assert by_kind["dependents_depends_on"]["value"] == 2
        # associative contrast, NEVER a dependency: refers_to(5)+mentions(1)=6
        assert by_kind["mentioned_by"]["value"] == 6
        # refers_to is NOT counted as a dependency
        assert "dependents_refers_to" not in by_kind
        # provenance capped, invariant holds
        assert len(by_kind["mentioned_by"]["sources"]) <= by_kind["mentioned_by"]["sources_total"]
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_build_claims_missing_node_is_empty():
    db = await _db()
    try:
        assert await _build_claims(db, "ghost", now=NOW) == []
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_build_claims_freshness_per_claim():
    old = _iso(NOW - timedelta(days=400))
    recent = _iso(NOW - timedelta(days=1))
    # stale subject (function budget 7d), never verified → needs_review True
    db1 = await _db(
        nodes=[("f", "function", old, None)],
        edges=[("a", "f", "depends_on", None, "x")],
    )
    # same staleness but re-verified yesterday → needs_review False
    db2 = await _db(
        nodes=[("f", "function", old, recent)],
        edges=[("a", "f", "depends_on", None, "x")],
    )
    try:
        c1 = (await _build_claims(db1, "f", now=NOW))[0]
        c2 = (await _build_claims(db2, "f", now=NOW))[0]
        assert c1["needs_review"] is True
        assert c2["needs_review"] is False
        assert c2["verified"] == recent
    finally:
        await db1.close()
        await db2.close()


# --- model + flag ----------------------------------------------------------

def test_kgclaim_sources_invariant():
    with pytest.raises(Exception):
        KGClaim(kind="k", subject="s", value=5, sources=["a", "b", "c"], sources_total=2)


def test_kg_claims_flag_default_off():
    assert settings.kg_claims_enabled is False
