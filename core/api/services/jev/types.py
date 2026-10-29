# v1.0.0 - 2026-09-21 - Plan 2026-09-21 U1: shared types for the Jev scoring port.
"""Types of the ``ScoringProvider`` port (plan KTD1).

Callers depend on these types and on :class:`ScoringProvider`, never on the
Jev HTTP client: the whole failure policy (timeout, breaker, ceiling, flag,
version pin, schema validation) lives only inside the adapter
(:mod:`core.api.services.jev.client`).

``score()`` returns ``None`` to mean *unavailable* — the fail-closed contract:
the caller keeps its deterministic behaviour and never sees an exception from
the provider (plan R17).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

QuestionType = str  # "noul" | "choice" | "score" — provider-side vocabulary


def should_act_on_low(
    score: float, confidence: float, *, below: float, confidence_min: float
) -> bool:
    """The one decision rule every advisory surface shares (plan KTD4).

    Act (demote / omit) ONLY when the value is clearly low AND the model is
    confident: a low-confidence answer is "I cannot tell", not a judgment.
    The calibration harness (eval/jev_calibration.py) replays THIS function
    against owner labels, so the rule has exactly one home.
    """
    return score < below and confidence >= confidence_min


@dataclass(frozen=True)
class ScoringQuestion:
    """One typed question asked about the shared state (provider primitives).

    ``instructions`` carries the full question text; ``criteria`` is the
    option map (choice), the ordered levels (score) or the true/false
    clarification (noul, optional).
    """

    id: str
    type: QuestionType
    instructions: str
    criteria: Any | None = None

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "type": self.type,
            "instructions": self.instructions,
        }
        if self.criteria is not None:
            payload["criteria"] = self.criteria
        return payload


@dataclass
class ScoringAnswer:
    """One typed answer: a noul probability, a choice pick, or a rubric score.

    ``confidence`` is ``None`` for noul answers (the provider does not emit
    one for them); callers gate on it per plan R16/R17. Not frozen: the
    validator fills the typed fields progressively.
    """

    type: QuestionType
    noul: float | None = None
    choice: str | None = None
    score: float | None = None
    confidence: float | None = None
    probabilities: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class ScoringBatchResult:
    """Validated result of ONE batch request (N questions, one state)."""

    model: str
    answers: dict[str, ScoringAnswer]
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0


@runtime_checkable
class ScoringProvider(Protocol):
    """The port every Jev-aware surface calls (plan KTD1).

    ``state`` is the serialized facts being judged; ``questions`` is the fan-out
    batch (one request carries all of them — plan KTD1 batch contract).
    Returns ``None`` on ANY unavailability (flag off, breaker open, ceiling
    hit, timeout, malformed/foreign-version response): fail-closed, never an
    exception.
    """

    async def score(
        self,
        state: Any,
        questions: list[ScoringQuestion],
        *,
        tenant_id: str | None = None,
        surface: str = "unknown",
    ) -> ScoringBatchResult | None: ...

    async def score_with_outcome(
        self,
        state: Any,
        questions: list[ScoringQuestion],
        *,
        tenant_id: str | None = None,
        surface: str = "unknown",
    ) -> tuple[ScoringBatchResult | None, str]: ...


__all__ = [
    "QuestionType",
    "ScoringAnswer",
    "ScoringBatchResult",
    "ScoringProvider",
    "ScoringQuestion",
]
