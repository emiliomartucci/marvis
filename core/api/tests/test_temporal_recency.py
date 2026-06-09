"""Fase D producer — KG temporal recency pass (build_recency_drafts + apply guidance).

Pure-logic coverage: the rule emits guidance-only `reinforce`/`none` proposals for
aging unverified nodes, and the apply guidance routes them to mark_kg_verified.
The DB scan + persist path is the warehouse_consolidate-proven plumbing.
"""
from __future__ import annotations

from datetime import datetime, timezone

import aiosqlite
import pytest

from core.api.services.brain.memory_ops import _next_action_for, finalize_operation
from core.api.services.brain.temporal_recency import (
    _NodeRow,
    _scan_query,
    build_recency_drafts,
)

NOW = datetime(2026, 6, 8, tzinfo=timezone.utc)

_SCAN_SCHEMA = """
CREATE TABLE graph_nodes (
    id TEXT PRIMARY KEY, type TEXT, project_id TEXT,
    deprecated_at TEXT, last_verified_at TEXT, last_seen_at TEXT
);
CREATE TABLE brain_memory_operations (
    operation_id TEXT PRIMARY KEY, source_ref TEXT, operation_type TEXT,
    proposed_write_target_type TEXT, approval_state TEXT, detected_at TEXT
);
"""

# One live, unverified, aging node (last seen ~100d before NOW).
_AGING_NODE = ("py:function:a", "function", "p", None, None, "2026-02-28 10:00:00")


async def _scan_db(ops: list[tuple]):
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await conn.executescript(_SCAN_SCHEMA)
    await conn.execute(
        "INSERT INTO graph_nodes (id,type,project_id,deprecated_at,last_verified_at,last_seen_at)"
        " VALUES (?,?,?,?,?,?)",
        _AGING_NODE,
    )
    for o in ops:
        await conn.execute(
            "INSERT INTO brain_memory_operations "
            "(operation_id,source_ref,operation_type,proposed_write_target_type,approval_state,detected_at)"
            " VALUES (?,?,?,?,?,?)",
            o,
        )
    await conn.commit()
    return conn


async def _scan_ids(ops: list[tuple]) -> list[str]:
    sql, params = _scan_query(now=NOW)
    db = await _scan_db(ops)
    try:
        rows = await (await db.execute(sql, params)).fetchall()
        return [r["id"] for r in rows]
    finally:
        await db.close()


def test_build_recency_drafts_emits_reinforce_none():
    nodes = [
        _NodeRow("py:function:a", "function", "marvisx", "2026-05-01 10:00:00"),
        _NodeRow("py:file:b", "file", None, "2026-04-01 10:00:00"),
    ]
    drafts = build_recency_drafts(nodes, now=NOW)
    assert len(drafts) == 2
    for d in drafts:
        assert d.operation_type == "reinforce"
        assert d.proposed_write.target_type == "none"
        assert d.target_ref == ""
        # node id rides in source_ref; evidence is the single stable ref
        assert d.evidence == [f"kg_node:{d.source_ref}"]
        assert 0.0 <= d.score <= 1.0


def test_build_recency_drafts_is_deterministic_sorted():
    nodes = [
        _NodeRow("py:function:z", "function", "p", "2026-05-01 10:00:00"),
        _NodeRow("py:file:a", "file", "p", "2026-04-01 10:00:00"),
    ]
    drafts = build_recency_drafts(nodes, now=NOW)
    assert [d.source_ref for d in drafts] == ["py:file:a", "py:function:z"]


def test_build_recency_drafts_empty():
    assert build_recency_drafts([], now=NOW) == []


def test_recency_apply_guidance_points_to_mark_verified():
    """The closing of the loop: an approved recency reinforce tells the agent to
    call mark_kg_verified with the node id — guidance-only, no auto-write."""
    draft = build_recency_drafts(
        [_NodeRow("py:function:a", "function", "marvisx", "2026-04-01 10:00:00")],
        now=NOW,
    )[0]
    op = finalize_operation(draft=draft, run_id="run1", cycle_key="2026-06-08", now=NOW)
    action = _next_action_for(op)
    assert action.tool == "mcp__marvis__mark_kg_verified"
    assert action.args == {"node_id": "py:function:a"}
    assert "last_verified_at" in action.rationale


def test_recency_op_id_is_idempotent_across_runs():
    """Same node → same evidence → same operation_id within a cycle (dedup)."""
    nodes = [_NodeRow("py:function:a", "function", "marvisx", "2026-04-01 10:00:00")]
    d1 = build_recency_drafts(nodes, now=NOW)[0]
    d2 = build_recency_drafts(nodes, now=NOW)[0]
    op1 = finalize_operation(draft=d1, run_id="r1", cycle_key="2026-06-08", now=NOW)
    op2 = finalize_operation(draft=d2, run_id="r2", cycle_key="2026-06-08", now=NOW)
    assert op1.operation_id == op2.operation_id


@pytest.mark.asyncio
async def test_scan_picks_aging_unverified_node():
    """No prior proposal → the aging unverified node is a candidate."""
    assert await _scan_ids([]) == ["py:function:a"]


@pytest.mark.asyncio
async def test_scan_suppresses_node_with_pending_proposal():
    """A still-pending recency proposal suppresses re-emission (no nightly dup)."""
    ops = [("op1", "py:function:a", "reinforce", "none", "pending", "2026-06-07 10:00:00")]
    assert await _scan_ids(ops) == []


@pytest.mark.asyncio
async def test_scan_suppresses_recent_dismiss_but_not_old():
    """A recent dismiss (within window) backs off; an old dismiss lets it return."""
    recent = [("op1", "py:function:a", "reinforce", "none", "dismissed", "2026-06-07 10:00:00")]
    old = [("op1", "py:function:a", "reinforce", "none", "dismissed", "2026-04-01 10:00:00")]
    assert await _scan_ids(recent) == []
    assert await _scan_ids(old) == ["py:function:a"]


@pytest.mark.asyncio
async def test_scan_ignores_unrelated_edge_metric_reinforce():
    """An M1 edge-metric reinforce (target_type != 'none') must NOT suppress the
    recency proposal — the guard is scoped to recency ops only."""
    ops = [("op1", "py:function:a", "reinforce", "kg_edge_metric", "pending", "2026-06-07 10:00:00")]
    assert await _scan_ids(ops) == ["py:function:a"]
