"""Unit test for the pre-aggregated neighbour summary (#1a — at-scale faithfulness).

The agent must be able to QUOTE counts from the tool result instead of re-counting
the neighbour list by hand (the documented LLM miscount failure mode).
"""

from __future__ import annotations

from core.api.use_cases.graph import _neighbor_summary


def test_neighbor_summary_counts():
    raw = [
        {"node_id": "a", "relation": "depends_on", "direction": "incoming"},
        {"node_id": "b", "relation": "depends_on", "direction": "incoming"},
        {"node_id": "a", "relation": "mentions", "direction": "outgoing"},  # dup node a
        {"missing_fields": True},  # dict but no relation/direction/node → only counts in total
        "not-a-dict",  # ignored
    ]
    s = _neighbor_summary(raw)
    assert s["total"] == 5
    assert s["distinct_nodes"] == 2  # a, b
    assert s["by_relation"] == {"depends_on": 2, "mentions": 1}
    assert s["by_direction"] == {"incoming": 2, "outgoing": 1}
    assert "cite" in s["note"].lower()


def test_neighbor_summary_empty():
    s = _neighbor_summary([])
    assert s["total"] == 0
    assert s["distinct_nodes"] == 0
    assert s["by_relation"] == {}
