"""Fase 3 — de-noise graph_neighbors (closes the S1/S2 regression).

The per-neighbour `needs_review` flag is signal only as a MINORITY hint on a fresh
index. When the index is globally stale or most neighbours are flagged, the flag is
wallpaper (127/200 marked drowned the task): it is stripped from every neighbour and
the count is stated ONCE in the summary. `derivation` is left intact. Flag-off
(MARVIS_TEMPORAL_MEMORY off) is byte-identical — nothing here runs.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import core.api.use_cases.graph as graph_mod
from core.api.config import settings
from core.api.services import graph_service
from core.api.use_cases import graph as uc
from core.api.use_cases._context import CallerContext
from core.api.use_cases.graph import (
    _apply_freshness,
    _gate_needs_review_noise,
    _neighbor_summary,
)

CTX = CallerContext.local_single_user()
SUBJECT = "py:function:hub"


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _nb(stale: bool, idx: int) -> dict:
    """A neighbour fixture in the prod shape `_apply_freshness` reads. Stale = old
    observation stamp (function budget 7d); fresh = yesterday."""
    now = datetime.now(timezone.utc)
    ts = _iso(now - timedelta(days=400 if stale else 1))
    return {
        "id": f"py:function:n{idx}",
        "type": "function",
        "relation": "calls",
        "direction": "incoming",
        "freshness": {"node_updated_at": ts, "edge_last_seen_at": ts, "node_last_verified_at": None},
    }


def _raw(n_stale: int, n_fresh: int) -> list:
    return [_nb(True, i) for i in range(n_stale)] + [_nb(False, 100 + i) for i in range(n_fresh)]


# --------------------------------------------------------------------------- #
# Pure gate logic                                                             #
# --------------------------------------------------------------------------- #


def test_gate_suppresses_when_majority_flagged():
    raw = _raw(n_stale=4, n_fresh=1)
    n_review = _apply_freshness(raw)
    assert n_review == 4  # 4/5 = 0.8 > 0.5
    suppressed = _gate_needs_review_noise(raw, n_review, index_stale=False)
    assert suppressed is True
    assert all("needs_review" not in n for n in raw)
    assert all(n["derivation"] == "observed" for n in raw)  # derivation kept


def test_gate_suppresses_when_index_stale_even_if_minority():
    raw = _raw(n_stale=1, n_fresh=4)
    n_review = _apply_freshness(raw)
    assert n_review == 1  # minority, but...
    suppressed = _gate_needs_review_noise(raw, n_review, index_stale=True)
    assert suppressed is True  # global staleness already in `freshness`
    assert all("needs_review" not in n for n in raw)


def test_gate_keeps_minority_signal_on_fresh_index():
    raw = _raw(n_stale=1, n_fresh=4)
    n_review = _apply_freshness(raw)
    assert n_review == 1  # 1/5 = 0.2 < 0.5
    suppressed = _gate_needs_review_noise(raw, n_review, index_stale=False)
    assert suppressed is False
    flagged = [n for n in raw if n.get("needs_review")]
    assert len(flagged) == 1  # the genuine minority hint survives


def test_gate_no_flags_no_suppression():
    raw = _raw(n_stale=0, n_fresh=3)
    n_review = _apply_freshness(raw)
    assert n_review == 0
    assert _gate_needs_review_noise(raw, n_review, index_stale=False) is False


def test_summary_note_only_when_suppressed():
    raw = [{"id": "a", "relation": "calls", "direction": "incoming"}]
    s_plain = _neighbor_summary(raw, n_needs_review=5)
    assert s_plain["needs_review"] == 5
    assert "needs_review_note" not in s_plain
    s_supp = _neighbor_summary(raw, n_needs_review=5, needs_review_suppressed=True)
    assert s_supp["needs_review"] == 5  # count still stated once
    assert "needs_review_note" in s_supp


# --------------------------------------------------------------------------- #
# End-to-end through graph_neighbors (heavy deps stubbed)                     #
# --------------------------------------------------------------------------- #


def _patch(monkeypatch, *, raw_factory, stale):
    async def _stub_neighbors(db, **kw):
        return raw_factory()

    async def _stub_fresh(db, **kw):
        return {"stale": stale, "reason": "behind_head" if stale else "fresh"}

    monkeypatch.setattr(graph_service, "get_neighbors_with_metadata", _stub_neighbors)
    monkeypatch.setattr(graph_mod, "compute_index_freshness", _stub_fresh)


@pytest.mark.asyncio
async def test_neighbors_flag_off_is_byte_identical(monkeypatch):
    monkeypatch.setattr(settings, "temporal_memory_enabled", False)
    # flag-off: the real service would not attach freshness; mirror that.
    _patch(
        monkeypatch,
        raw_factory=lambda: [
            {"id": "py:function:n0", "relation": "calls", "direction": "incoming"}
        ],
        stale=False,
    )
    res = await uc.graph_neighbors(CTX, None, node_id=SUBJECT)
    assert "needs_review" not in res["summary"]
    assert "needs_review_note" not in res["summary"]
    assert "derivation" not in res["neighbors"][0]
    assert "needs_review" not in res["neighbors"][0]


@pytest.mark.asyncio
async def test_neighbors_flag_on_majority_suppressed(monkeypatch):
    monkeypatch.setattr(settings, "temporal_memory_enabled", True)
    _patch(monkeypatch, raw_factory=lambda: _raw(n_stale=4, n_fresh=1), stale=False)
    res = await uc.graph_neighbors(CTX, None, node_id=SUBJECT)
    assert res["summary"]["needs_review"] == 4  # count stated once
    assert "needs_review_note" in res["summary"]
    # not spammed on every neighbour; derivation still present
    assert all("needs_review" not in n for n in res["neighbors"])
    assert all("derivation" in n for n in res["neighbors"])


@pytest.mark.asyncio
async def test_neighbors_flag_on_minority_kept(monkeypatch):
    monkeypatch.setattr(settings, "temporal_memory_enabled", True)
    _patch(monkeypatch, raw_factory=lambda: _raw(n_stale=1, n_fresh=4), stale=False)
    res = await uc.graph_neighbors(CTX, None, node_id=SUBJECT)
    assert res["summary"]["needs_review"] == 1
    assert "needs_review_note" not in res["summary"]  # minority = genuine signal
    flagged = [n for n in res["neighbors"] if n.get("needs_review")]
    assert len(flagged) == 1


@pytest.mark.asyncio
async def test_neighbors_flag_on_index_stale_suppressed(monkeypatch):
    monkeypatch.setattr(settings, "temporal_memory_enabled", True)
    _patch(monkeypatch, raw_factory=lambda: _raw(n_stale=1, n_fresh=4), stale=True)
    res = await uc.graph_neighbors(CTX, None, node_id=SUBJECT)
    assert res["summary"]["needs_review"] == 1
    assert "needs_review_note" in res["summary"]  # index stale → suppressed
    assert all("needs_review" not in n for n in res["neighbors"])
