# v1.0.0 - 2026-06-05 - Track 2 #1-S4: LLM tiebreak for the supersede band (INTERFACE + STUB)
"""Write-time tiebreak seam for the 0.80-0.97 supersede band.

Track 2 #1-S4 (plan ``docs/plans/2026-06-04-track2-engine-moat-roadmap-plan.md``,
lines 327, 334, 349). This is the SEAM that lets a future, OFF-HOST local LLM
auto-resolve the high-confidence cases the S3 band today can only PROPOSE.

Context (what S3 ships, what S4 adds)
-------------------------------------
S3 (:mod:`core.api.services.temporal_write`): a new learning whose cosine to its
closest LIVE neighbour falls in ``[ADD_FLOOR, NOOP_CEIL] == [0.80, 0.97]`` ALWAYS
becomes a ``SUPERSEDE_CANDIDATE`` → a *pending* proposal in the
``brain_memory_operations`` approval gate. Nothing is auto-invalidated; a human
confirms. The band is the false-merge surface (two distinct learnings on the same
module sit ~0.90), so propose-only is the safe default.

S4 adds a RESOLVER seam so a local judge (Granite / tier-think) CAN auto-resolve
the band — but **only** high-confidence SUPERSEDE verdicts, and **only** once an
eval pass has driven the false-merge rate to ~0 (plan lines 340-343). Everything is
behind the SAME flag, ``settings.temporal_memory_enabled`` — S4 adds NO new flag.

HARD host constraint (why this file is INTERFACE + STUB)
--------------------------------------------------------
No model runs on the shared MarvisX host. Running a local embedding/LLM on this box
is exactly the incident in learning ``a09b8754`` (codex loaded 2.1GB of torch + ran
an embedding model → load 17.96, cascade). So:

* :class:`NoopResolver` is the DEFAULT and loads NOTHING. It always returns
  ``UNDECIDED`` → :func:`resolve_band` falls back to ``"propose"`` → behaviour is
  byte-for-byte identical to S3 (always propose, never auto-apply).
* :class:`LocalLLMResolver` is a STUB whose ``__init__`` raises
  ``NotImplementedError``. The intended wiring is documented on the class; it MUST
  NOT be instantiated until it runs off-host (Mac tier-think gateway) and an eval
  pass has gated auto-apply on.

The decision boundary itself (:func:`resolve_band`) is a PURE function: it never
touches a model, a DB, or I/O, so it is trivially unit-testable.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol, runtime_checkable

# ---------------------------------------------------------------------------
# Auto-apply threshold (PINNED). EVAL-GATED: this constant is the line above which
# a SUPERSEDE verdict is committed WITHOUT a human. It MUST stay effectively
# unreachable (the DEFAULT NoopResolver returns confidence 0.0 → never ≥ this)
# until a false-merge eval pass (plan lines 340-343: gold pairs hand-labelled
# {should-supersede, should-merge, distinct}; target false-merge rate ~0) proves a
# real resolver is safe. A false merge destroys data; a missed supersede is caught
# later by the dream cycle (S5). Tune the bands BEFORE raising auto-apply.
# ---------------------------------------------------------------------------

AUTO_APPLY_THRESHOLD: float = 0.90


class TiebreakAction(str, Enum):
    """What a resolver concluded for a learning sitting in the 0.80-0.97 band.

    * ``UPDATE``    — the new row refines the neighbour in place (treated as
                      ``"propose"`` for now: an in-place edit still goes through the
                      approval gate, never auto-applied at S4).
    * ``SUPERSEDE`` — the new row replaces the neighbour; the OLD row should be
                      soft-invalidated. The ONLY action eligible for auto-apply.
    * ``NOOP``      — near-duplicate / no new fact → keep the neighbour, drop the
                      new row. Never auto-applied via this seam.
    * ``UNDECIDED`` — the resolver declined / abstained (the DEFAULT). Always
                      routes to ``"propose"`` so the human decides.
    """

    UPDATE = "update"
    SUPERSEDE = "supersede"
    NOOP = "noop"
    UNDECIDED = "undecided"


@dataclass(frozen=True)
class TiebreakVerdict:
    """A resolver's conclusion for one band case.

    ``action``     — see :class:`TiebreakAction`.
    ``confidence`` — ``[0.0, 1.0]``. Compared against :data:`AUTO_APPLY_THRESHOLD`
                     by :func:`resolve_band`. The DEFAULT :class:`NoopResolver`
                     always reports ``0.0`` so nothing it returns is ever
                     auto-applied.
    """

    action: TiebreakAction
    confidence: float = 0.0


@runtime_checkable
class TiebreakResolver(Protocol):
    """The seam a band case is routed through.

    Implementations take the new row's text, the closest LIVE neighbour's text, and
    the cosine score that put the pair in the band, and return a
    :class:`TiebreakVerdict`. A resolver MUST be cheap to *acquire* (the DEFAULT
    loads nothing) — model loading, if any, belongs OFF-HOST inside the resolver,
    never on the write path of this host.
    """

    def resolve(
        self, new_text: str, neighbor_text: str, score: float
    ) -> TiebreakVerdict: ...


class NoopResolver:
    """DEFAULT resolver. Loads nothing, decides nothing → always ``UNDECIDED``.

    With this resolver wired in, :func:`resolve_band` always returns ``"propose"``,
    so the write path behaves EXACTLY like S3: every band case becomes a pending
    ``SUPERSEDE_CANDIDATE`` proposal and nothing is auto-applied. This is the safe
    production default until a real, off-host resolver is eval-gated on.
    """

    def resolve(
        self, new_text: str, neighbor_text: str, score: float
    ) -> TiebreakVerdict:
        return TiebreakVerdict(action=TiebreakAction.UNDECIDED, confidence=0.0)


class LocalLLMResolver:
    """STUB — a local LLM judge for the band. MUST run OFF-HOST. Not implemented.

    Instantiating this on the shared MarvisX host is forbidden: running a local
    Granite / tier-think model here is the exact failure in learning ``a09b8754``
    (an embedding/LLM loaded on this box drove load to 17.96 and cascaded). So the
    constructor raises and no model dependency is imported at module load.

    Intended wiring (when this ships, OFF-HOST + eval-gated):
      1. ``__init__(self, endpoint, *, model, timeout_s)`` — hold a handle to the
         REMOTE tier-think gateway (the Mac Studio LiteLLM endpoint), NOT an
         in-process model. No torch/transformers import on this host.
      2. ``resolve(new_text, neighbor_text, score)`` — build a strict
         classification prompt ("does the new fact SUPERSEDE / UPDATE / leave NOOP
         the neighbour?"), call the remote judge with a low temperature, parse a
         constrained ``{action, confidence}`` JSON, and map it onto
         :class:`TiebreakVerdict`. Any transport/parse failure → return
         ``UNDECIDED`` (fail-safe to propose-only, never auto-apply on doubt).
      3. Auto-apply stays OFF until a false-merge eval pass (plan lines 340-343)
         drives the band's false-merge rate to ~0 on the gold set.
    """

    def __init__(self, *args, **kwargs) -> None:
        raise NotImplementedError(
            "LocalLLMResolver must run OFF-HOST (remote tier-think gateway), never "
            "on the shared MarvisX host (learning a09b8754). It is also eval-gated: "
            "auto-apply must stay off until a false-merge eval pass. Use NoopResolver."
        )

    def resolve(
        self, new_text: str, neighbor_text: str, score: float
    ) -> TiebreakVerdict:  # pragma: no cover - unreachable: __init__ raises
        raise NotImplementedError


# Single DEFAULT instance the write path reaches for when no real resolver is
# configured. Cheap, stateless, loads nothing.
_DEFAULT_RESOLVER: TiebreakResolver = NoopResolver()


def get_tiebreak_resolver() -> TiebreakResolver:
    """Return the configured band resolver. DEFAULT = :class:`NoopResolver`.

    This is the single seam the write path calls. Today it always returns the
    no-op resolver, so the band stays propose-only (identical to S3). When a real,
    OFF-HOST, eval-gated resolver exists, swap it in HERE (and nowhere else) so the
    auto-apply decision flows through :func:`resolve_band` unchanged.
    """
    return _DEFAULT_RESOLVER


def resolve_band(
    verdict: TiebreakVerdict, *, auto_apply_threshold: float = AUTO_APPLY_THRESHOLD
) -> str:
    """Map a resolver verdict to the write-path action. PURE (no model, no DB).

    Returns one of two strings:

    * ``"apply"``   — ONLY for a high-confidence SUPERSEDE: ``action is SUPERSEDE``
                      AND ``confidence >= auto_apply_threshold``. The caller runs
                      :func:`temporal_write.apply_supersede` automatically.
    * ``"propose"`` — EVERYTHING else (``UNDECIDED``, a low-confidence SUPERSEDE,
                      ``NOOP``, ``UPDATE``). The caller runs
                      :func:`temporal_write.propose_supersede_candidate` — the S3
                      behaviour.

    The asymmetry is deliberate: auto-apply is the dangerous direction (a false
    merge destroys data), so it is gated hard; everything uncertain falls back to
    the human/approval path. With the DEFAULT :class:`NoopResolver` (confidence
    0.0), this can NEVER return ``"apply"``.
    """
    if (
        verdict.action is TiebreakAction.SUPERSEDE
        and verdict.confidence >= auto_apply_threshold
    ):
        return "apply"
    return "propose"
