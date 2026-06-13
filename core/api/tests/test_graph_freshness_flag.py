"""Fase A — read-time needs_review + derivation (at-scale freshness).

Unit coverage for the helper logic + the byte-identical-off guard at summary level
(no `needs_review` key unless explicitly passed → flag-off adds nothing).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from core.api.use_cases.graph import (
    _apply_freshness,
    _derivation_of,
    _needs_review,
    _neighbor_summary,
)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _nb(
    relation: str,
    node_type: str,
    node_ts: str | None = None,
    edge_ts: str | None = None,
    verified_ts: str | None = None,
) -> dict:
    n: dict = {
        "id": f"py:{node_type}:x",
        "type": node_type,
        "edge": {"relation": relation, "direction": "incoming"},
    }
    if node_ts or edge_ts or verified_ts:
        n["freshness"] = {
            "node_updated_at": node_ts,
            "edge_last_seen_at": edge_ts,
            "node_last_verified_at": verified_ts,
        }
    return n


def test_derivation_observed_vs_derived():
    assert _derivation_of("calls") == "observed"
    assert _derivation_of("imports") == "observed"
    assert _derivation_of("defines") == "observed"
    assert _derivation_of("mentions") == "derived"
    assert _derivation_of("similar_to") == "derived"
    assert _derivation_of(None) == "derived"


def test_needs_review_fresh_vs_stale_and_missing_and_future():
    now = datetime(2026, 6, 7, tzinfo=timezone.utc)
    recent = _iso(now - timedelta(days=1))
    old = _iso(now - timedelta(days=400))
    # observed code edge (budget 7d): fresh → no review, old → review
    assert _needs_review(_nb("calls", "function", recent, recent), now) is False
    assert _needs_review(_nb("calls", "function", old, old), now) is True
    # missing freshness → review (missing != fresh)
    assert _needs_review(_nb("mentions", "function"), now) is True
    # future-dated timestamp (clock skew) → review (never coerce to now)
    future = _iso(now + timedelta(days=5))
    assert _needs_review(_nb("calls", "function", future, future), now) is True
    # unparseable timestamp → treated as missing → review
    assert _needs_review(_nb("calls", "function", "not-a-date", None), now) is True


def test_needs_review_derived_tightens_budget():
    now = datetime(2026, 6, 7, tzinfo=timezone.utc)
    ts = _iso(now - timedelta(days=20))  # 20 days old; project budget = 30d
    # derived (mentions): budget 30*0.5 = 15 → 20 > 15 → review
    assert _needs_review(_nb("mentions", "project", ts, ts), now) is True
    # observed (calls): budget 30 → 20 < 30 → no review
    assert _needs_review(_nb("calls", "project", ts, ts), now) is False


def test_apply_freshness_decorates_strips_and_counts():
    now = datetime.now(timezone.utc)
    recent = _iso(now - timedelta(days=1))
    old = _iso(now - timedelta(days=400))
    raw = [
        _nb("calls", "function", recent, recent),  # fresh
        _nb("calls", "function", old, old),         # stale
        _nb("mentions", "function"),                 # missing → review
    ]
    n_review = _apply_freshness(raw)
    assert n_review == 2
    for item in raw:
        assert "derivation" in item
        assert "needs_review" in item
        assert "freshness" not in item  # internal block stripped
    assert raw[0]["needs_review"] is False
    assert raw[0]["derivation"] == "observed"
    assert raw[2]["derivation"] == "derived"


def test_summary_needs_review_is_additive():
    """The byte-identical-off guard at summary level: no `needs_review` key
    unless explicitly passed (flag-off never passes it)."""
    raw = [{"id": "a", "edge": {"relation": "calls", "direction": "incoming"}}]
    s_off = _neighbor_summary(raw)
    assert "needs_review" not in s_off
    s_on = _neighbor_summary(raw, n_needs_review=3)
    assert s_on["needs_review"] == 3


def test_last_verified_at_refreshes_a_stale_node():
    """Fase D read-wiring: a node with OLD mechanical timestamps but a RECENT
    last_verified_at (Brain REINFORCE) is fresh — re-verification beats age."""
    now = datetime(2026, 6, 8, tzinfo=timezone.utc)
    old = _iso(now - timedelta(days=400))
    recent = _iso(now - timedelta(days=1))
    # stale observation, never verified → review
    assert _needs_review(_nb("mentions", "project", old, old), now) is True
    # same stale observation, but re-verified yesterday → fresh
    assert _needs_review(_nb("mentions", "project", old, old, verified_ts=recent), now) is False


def test_last_verified_at_future_is_not_fresh():
    """A future last_verified_at (clock skew) never coerces to fresh — it trips
    the same future-guard as any other stamp (Graphiti #1489)."""
    now = datetime(2026, 6, 8, tzinfo=timezone.utc)
    recent = _iso(now - timedelta(days=1))
    future = _iso(now + timedelta(days=5))
    assert _needs_review(_nb("calls", "function", recent, recent, verified_ts=future), now) is True


def test_last_verified_at_absent_is_backward_compatible():
    """Fase A neighbours without the new key behave exactly as before."""
    now = datetime(2026, 6, 8, tzinfo=timezone.utc)
    recent = _iso(now - timedelta(days=1))
    old = _iso(now - timedelta(days=400))
    # no verified_ts passed → freshness dict lacks the key → unchanged behaviour
    assert _needs_review(_nb("calls", "function", recent, recent), now) is False
    assert _needs_review(_nb("calls", "function", old, old), now) is True
