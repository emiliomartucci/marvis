"""Fase B — per-relation half-life + decay floor in project-edge aggregation.

Unit coverage for the pure `_recency_decay` helper:
  - flag-off = byte-identical legacy (uniform 180d half-life, no floor)
  - flag-on  = per-relation half-life + per-relation floor (durable facts don't
    collapse to ~0 from age alone)
"""

from __future__ import annotations

import math

from core.scripts.populate_project_nodes import (
    DECAY_FLOOR,
    HALF_LIFE_BY_RELATION,
    HALF_LIFE_DAYS,
    _recency_decay,
)


def test_decay_flag_off_is_legacy_uniform():
    """flag-off: every relation uses the uniform 180d half-life and NO floor —
    byte-identical to the pre-Fase-B `exp(-age_days / 180)`."""
    for rel in ("depends_on", "applies_to", "refers_to", "mentions", "cites", "calls"):
        assert _recency_decay(rel, 0.0, False) == 1.0
        assert _recency_decay(rel, HALF_LIFE_DAYS, False) == math.exp(-1.0)
        # very old: legacy lets it decay toward zero (no floor)
        legacy_old = _recency_decay(rel, 10_000.0, False)
        assert legacy_old == math.exp(-10_000.0 / HALF_LIFE_DAYS)
        assert legacy_old < 1e-20


def test_decay_flag_on_per_relation_rates():
    """flag-on: same age, different relation → different decay. depends_on
    (hl 30) ages faster than refers_to (hl 180)."""
    age = 60.0
    d_dep = _recency_decay("depends_on", age, True)
    d_ref = _recency_decay("refers_to", age, True)
    assert d_dep < d_ref
    # explicit half-life values are honored
    assert _recency_decay("depends_on", HALF_LIFE_BY_RELATION["depends_on"], True) == math.exp(-1.0)
    assert _recency_decay("refers_to", HALF_LIFE_BY_RELATION["refers_to"], True) == math.exp(-1.0)


def test_decay_flag_on_floor_prevents_zero():
    """flag-on: a durable old fact is held at its per-relation floor, never ~0.
    flag-off at the same age decays below the floor (proves the floor is the
    flag-on behaviour, not legacy)."""
    very_old = 100_000.0
    on = _recency_decay("refers_to", very_old, True)
    assert on == DECAY_FLOOR["refers_to"]
    assert on > 0.0
    off = _recency_decay("refers_to", very_old, False)
    assert off < DECAY_FLOOR["refers_to"]


def test_decay_flag_on_unknown_relation_falls_back():
    """flag-on: a relation absent from the tables falls back to HALF_LIFE_DAYS
    and floor 0 (no floor) — safe default."""
    assert _recency_decay("cites", HALF_LIFE_DAYS, True) == math.exp(-1.0)
    assert _recency_decay("cites", 10_000.0, True) == math.exp(-10_000.0 / HALF_LIFE_DAYS)


def test_decay_fresh_is_one_both_modes():
    """age 0 → full weight in both modes (floor never caps the top)."""
    assert _recency_decay("depends_on", 0.0, True) == 1.0
    assert _recency_decay("depends_on", 0.0, False) == 1.0
