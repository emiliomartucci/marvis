# v1.0.0 - 2026-06-04 - Track 2 #2: NLI verifier interface (Noop default + MiniCheck stub)
"""Grounding verifier interface (Track 2 #2).

The VERIFY step of select-then-generate-then-verify: per sentence, run one binary
NLI ("is this claim supported by its cited spans?") forward pass — "``check_safety``
applied to the facts". This module defines the INTERFACE only:

* ``Verifier`` Protocol — ``verify(hypothesis, premise) -> Verdict``.
* ``NoopVerifier`` — the DEFAULT. Loads nothing, returns an "unverified"
  passthrough verdict, so NOTHING in this layer requires a model. With it,
  ``decide`` sees ``entailment_verdict=False`` and routes cited sentences to HEDGE
  ("(non verificato)") rather than asserting a verification it never did.
* ``MiniCheckVerifier`` — a documented STUB. It is the place where MiniCheck
  (a small local NLI head) would be wired off-host, DELIBERATELY, by Emilio. Its
  ``__init__`` raises ``NotImplementedError`` — it never imports torch, never
  downloads a model, never runs inference (learning a09b8754: a model load
  saturates the shared host; the real incident class this whole section fixes must
  not be re-created by the fix itself).

NO model is imported, downloaded or run anywhere in this module.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

__all__ = [
    "Verdict",
    "Verifier",
    "NoopVerifier",
    "MiniCheckVerifier",
]


@dataclass(frozen=True, slots=True)
class Verdict:
    """One NLI verdict.

    * ``entailed`` — binary support decision (the MiniCheck calibrated classifier
      thresholds support-probability at ~0.5; do NOT tune a cosine threshold).
    * ``score`` — support probability in ``[0, 1]`` when the head produces one;
      ``None`` for a passthrough verdict (e.g. ``NoopVerifier``) that did not run a
      model and therefore has no calibrated score to report.
    * ``verified`` — whether a real verifier actually ran. ``False`` distinguishes
      "a model ran and said not-entailed" from "no model ran" (NoopVerifier), which
      the renderer needs to choose between "(non verificato)" and a hard drop.
    """

    entailed: bool
    score: float | None = None
    verified: bool = True


@runtime_checkable
class Verifier(Protocol):
    """A binary NLI head: is ``hypothesis`` entailed by ``premise``?

    ``premise`` is the concatenation of the hypothesis sentence's OWN cited spans
    (VeriCite multi-premise). One forward pass per sentence — NEVER the reasoning
    LLM as the NLI head (that doubles latency). Implementations should be cheap,
    local and bounded; verdicts may be cached by ``(sentence_hash, span_hashes)``.
    """

    def verify(self, hypothesis: str, premise: str) -> Verdict:
        ...


class NoopVerifier:
    """The DEFAULT verifier: loads nothing, verifies nothing.

    Returns ``entailed=False, score=None, verified=False`` — a pure passthrough.
    This keeps the entire grounding layer model-free by default: with the flag off
    the seam is never entered, and with the flag on but no NLI head wired, cited
    sentences route to HEDGE ("(non verificato)") instead of claiming a verification
    that never happened. Idempotent, side-effect-free, safe on any host.
    """

    def verify(self, hypothesis: str, premise: str) -> Verdict:
        return Verdict(entailed=False, score=None, verified=False)


class MiniCheckVerifier:
    """STUB: wrap MiniCheck as the local NLI head — NOT wired here.

    MiniCheck (arXiv:2404.10774) is the realistic LOCAL NLI head for fact-checking:
    one yes/no "is this claim supported by the doc?" forward pass. Two sizes —
    ``DeBERTa-v3-Large`` (~0.4B, CPU-usable) and ``Flan-T5-Large`` (~0.8B, GPU) —
    on par with GPT-4 at roughly 400x lower cost, >500 docs/min (~8 claims/s on one
    GPU). The calibrated binary classifier is thresholded at support-prob ~0.5; do
    NOT tune a cosine threshold.

    Wiring it is a DELIBERATE, off-host decision for Emilio. It MUST NOT run on the
    shared MarvisX host (learning a09b8754: a torch/ONNX load there saturates RAM +
    swap and cascades sessions). Intended wiring, when done off-host:

    1. Install MiniCheck + its model weights on a box with budget (GPU for
       Flan-T5-Large, or CPU-only for DeBERTa-0.4B), NOT inside the API process on
       the shared host.
    2. Implement ``verify(hypothesis, premise)`` as one forward pass:
       ``hypothesis`` = the brief sentence, ``premise`` = the concatenation of its
       cited spans. Return ``Verdict(entailed=prob>=0.5, score=prob, verified=True)``.
    3. Cache verdicts by ``(sentence_hash, span_hashes)`` to stay within the
       ~3-5s/brief latency budget.

    Until then this class is an interface placeholder: constructing it raises so it
    can never silently start a model.
    """

    def __init__(self, *args: object, **kwargs: object) -> None:
        raise NotImplementedError(
            "MiniCheckVerifier is a stub: install + wire MiniCheck off-host "
            "(NOT on the shared MarvisX host — see learning a09b8754). "
            "See the class docstring for the deliberate wiring steps. "
            "Until then use NoopVerifier (the default)."
        )

    def verify(self, hypothesis: str, premise: str) -> Verdict:  # pragma: no cover
        raise NotImplementedError("MiniCheckVerifier is not wired; see __init__.")
