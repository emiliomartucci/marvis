# v1.0.0 - 2026-05-29 - open-core funnel Phase 3 seam: is_pro() (free fallback, no machine)
"""The PRO-gating seam. Today everything is free: :func:`is_pro` always returns False.

Deliberately a SEAM, not a machine (Emilio 2026-05-29: no PRO feature at launch).
There is NO network call, NO signed cert, NO entitlement fetch here — building that
now would be dead code gating nothing (the retired-integration lesson: removing gating ceremony
built too early). When the first PRO feature + price exist, THIS is the single place to
wire the cloud-issued, locally-verified Ed25519 entitlement check (plan Deepening §C).

Two load-bearing rules for whoever lights this up later:
- **FAIL-OPEN**: a missing / unverifiable / expired entitlement degrades to free
  (``is_pro`` -> False). The OSS never locks a user out of free functionality.
- **Clean boundary**: free code calls ``is_pro`` and always has a runnable free path;
  free code must never import a PRO module (keeps the OSS build standalone).
"""
from __future__ import annotations


def is_pro(feature: str | None = None) -> bool:
    """True iff ``feature`` is unlocked on a paid tier. Always False today (all-free).

    The single gate a future PRO feature wraps itself in:
    ``if is_pro("hosted-console"): ... else: <free fallback>``. Until a PRO feature
    exists, this returns False unconditionally and the free fallback always runs.
    """
    return False
