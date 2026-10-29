# v1.0.0 - 2026-09-22 - Plan SP-C (R5/R6/R13/R15): guardrail + delegation + dispatch.
"""SP-C — three advisory surfaces for agent safety and routing.

R5/R6 **Guardrail** (``evaluate_action``): ONE batch (Noul destructive +
Choice verdict) on a proposed action. The verdict is allow / ask_human /
block; a low-confidence Choice degrades to ask_human (R6 safe default).
ADVISORY: the CALLER decides what to do with the verdict — this module
never blocks anything by itself (KTD4). The natural caller is an MCP tool
that executes side effects (bash commands, deploy, external sends) BEFORE
running them.

R13 **Delegation prefill** (``suggest_delegation``): ONE batch (Choice
delegate_to + Noul production_stakes) on a task description at creation.
Returns a suggested delegation with confidence; never changes the task field.

R15 **Intent dispatch** (``dispatch_intent``): ONE Choice on a user query
to route it to the best Marvis surface. Low confidence → proposes multiple
surfaces instead of picking one (R15).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("marvis.jev.guardrail")

_VERDICT_CONF_MIN = 0.5  # below this → ask_human (R6)
_ACTION_CAP = 400

# R15: the closed surface vocabulary (the well-known tier0 tools).
_DISPATCH_SURFACES = {
    "search": "Semantic search across all artifacts",
    "check_learnings": "Prevention rules matched to a concrete risk",
    "graph_impact": "Transitive blast radius in the knowledge graph",
    "list_tasks": "Filtered task listing",
    "session_brief": "Cold-start context bundle for a project",
    "guide": "Routing cheatsheet for all tools",
}


@dataclass
class GuardrailVerdict:
    verdict: str  # allow | ask_human | block
    destructive: float  # noul probability
    confidence: float | None
    outcome: str  # R16
    reasoning: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "surface": "guardrail",
            "verdict": self.verdict,
            "destructive": round(self.destructive, 2),
            "confidence": self.confidence,
            "outcome": self.outcome,
            "reasoning": self.reasoning,
        }


@dataclass
class DelegationSuggestion:
    delegate_to: str  # agent | hybrid | human
    production_stakes: float  # noul
    confidence: float | None
    outcome: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "surface": "delegation_prefill",
            "delegate_to": self.delegate_to,
            "production_stakes": round(self.production_stakes, 2),
            "confidence": self.confidence,
            "outcome": self.outcome,
        }


@dataclass
class DispatchResult:
    surface: str
    alternatives: list[str] = field(default_factory=list)
    confidence: float | None = None
    outcome: str = "ok"

    def to_dict(self) -> dict[str, Any]:
        return {
            "surface": "intent_dispatch",
            "best": self.surface,
            "alternatives": self.alternatives,
            "confidence": self.confidence,
            "outcome": self.outcome,
        }


async def evaluate_action(
    action: str,
    context: str = "",
    *,
    tenant_id: str | None = None,
    provider: Any = None,
    verdict_conf_min: float | None = None,
) -> GuardrailVerdict:
    """R5/R6: ONE batch (Noul destructive + Choice verdict) on a proposed action."""
    from core.api import config as _config
    from core.api.services.jev import load_provider
    from core.api.services.jev.types import ScoringQuestion
    from core.api.services.tool_usage import resolve_tenant_id

    settings = _config.settings
    conf_min = (
        settings.typesafe_guardrail_confidence_min
        if verdict_conf_min is None
        else verdict_conf_min
    )

    if len(action) > _ACTION_CAP or len(context) > 200:
        return GuardrailVerdict(
            "ask_human", 0.5, None, "error",
            "input exceeds scoring limit; the complete action was not judged",
        )

    provider = provider or load_provider()
    if provider is None:
        return GuardrailVerdict("ask_human", 0.5, None, "disabled_tenant", "provider unavailable")

    questions = [
        ScoringQuestion(
            id="destructive",
            type="noul",
            instructions=(
                "Does this action destroy, leak, or irreversibly mutate "
                "production state?"
            ),
        ),
        ScoringQuestion(
            id="verdict",
            type="choice",
            instructions=(
                f"Action: `{action[:_ACTION_CAP]}`. Context: `{context[:200]}`. "
                "What should the harness do before running it?"
            ),
            criteria={
                "allow": "Safe and reversible: run without asking",
                "ask_human": "Needs an owner confirmation first",
                "block": "Must never run unattended",
            },
        ),
    ]
    state = {"action": action[:_ACTION_CAP], "context": context[:200]}
    result, outcome = await provider.score_with_outcome(
        state,
        questions,
        tenant_id=tenant_id or resolve_tenant_id(None),
        surface="guardrail",
    )
    if result is None:
        return GuardrailVerdict("ask_human", 0.5, None, outcome, f"provider {outcome}")

    destructive = result.answers.get("destructive")
    noul = destructive.noul if destructive and destructive.noul is not None else 0.5

    verdict_answer = result.answers.get("verdict")
    if verdict_answer is None or verdict_answer.choice is None:
        return GuardrailVerdict("ask_human", noul, None, outcome, "verdict not judged")

    raw_verdict = verdict_answer.choice
    confidence = verdict_answer.confidence
    # R6: low-confidence verdict degrades to ask_human
    if confidence is None:
        final = "ask_human"
        reason = "verdict confidence unavailable; ask_human"
    elif confidence < conf_min:
        final = "ask_human"
        reason = f"verdict {raw_verdict} at conf {confidence:.2f} < {conf_min} → ask_human"
    else:
        final = raw_verdict
        reason = f"verdict {raw_verdict} (conf {confidence or '?'})"

    from core.api.services.jev.outbound import contains_credentials
    if final == "allow" and (contains_credentials(action) or contains_credentials(context)):
        final = "ask_human"
        reason = "credential redaction prevents assessment of the complete action"

    return GuardrailVerdict(final, noul, confidence, outcome, reason)


async def suggest_delegation(
    task_title: str,
    task_description: str = "",
    *,
    tenant_id: str | None = None,
    provider: Any = None,
    confidence_min: float = _VERDICT_CONF_MIN,
) -> DelegationSuggestion:
    """R13: ONE batch (Choice delegate_to + Noul production_stakes) on a task."""
    from core.api.services.jev import load_provider
    from core.api.services.jev.types import ScoringQuestion
    from core.api.services.tool_usage import resolve_tenant_id

    provider = provider or load_provider()
    if provider is None:
        return DelegationSuggestion("hybrid", 0.5, None, "disabled_tenant")

    questions = [
        ScoringQuestion(
            id="delegate_to",
            type="choice",
            instructions=(
                f"Task: `{task_title[:200]}`. `{task_description[:300]}`. "
                "Who should execute it end-to-end?"
            ),
            criteria={
                "agent": "Fully autonomous: mechanical, reversible, verifiable",
                "hybrid": (
                    "Agent executes end-to-end, credentials and production "
                    "operations included, with the owner supervising and "
                    "reviewing the result"
                ),
                "human": (
                    "Only what an agent cannot do at all: an owner decision "
                    "still open (choosing, approving, spending), an in-person "
                    "identity act, or a signature. Touching production or "
                    "credentials alone is NOT human"
                ),
            },
        ),
        ScoringQuestion(
            id="production_stakes",
            type="noul",
            instructions=(
                f"Does the task `{task_title[:200]}` touch production, "
                "credentials, or irreversible state?"
            ),
        ),
    ]
    state = {"title": task_title[:200], "description": task_description[:300]}
    result, outcome = await provider.score_with_outcome(
        state,
        questions,
        tenant_id=tenant_id or resolve_tenant_id(None),
        surface="delegation_prefill",
    )
    if result is None:
        return DelegationSuggestion("hybrid", 0.5, None, outcome)

    delegation = result.answers.get("delegate_to")
    stakes = result.answers.get("production_stakes")
    confident = (
        delegation is not None and delegation.confidence is not None
        and delegation.confidence >= confidence_min
    )
    return DelegationSuggestion(
        delegation.choice if confident and delegation.choice else "hybrid",
        stakes.noul if stakes and stakes.noul is not None else 0.5,
        delegation.confidence if delegation else None,
        outcome,
    )


async def dispatch_intent(
    query: str,
    *,
    tenant_id: str | None = None,
    provider: Any = None,
    conf_min: float = 0.5,
) -> DispatchResult:
    """R15: ONE Choice routing a query to the best surface. Low conf → multi-propose."""
    from core.api.services.jev import load_provider
    from core.api.services.jev.types import ScoringQuestion
    from core.api.services.tool_usage import resolve_tenant_id

    provider = provider or load_provider()
    if provider is None:
        return DispatchResult("guide", list(_DISPATCH_SURFACES.keys()), None, "disabled_tenant")

    question = ScoringQuestion(
        id="surface",
        type="choice",
        instructions=f"User query: `{query[:300]}`. Which single Marvis tool serves it best?",
        criteria=_DISPATCH_SURFACES,
    )
    result, outcome = await provider.score_with_outcome(
        {"query": query[:300]},
        [question],
        tenant_id=tenant_id or resolve_tenant_id(None),
        surface="intent_dispatch",
    )
    if result is None:
        return DispatchResult("guide", list(_DISPATCH_SURFACES.keys()), None, outcome)

    answer = result.answers.get("surface")
    if answer is None or answer.choice is None:
        return DispatchResult("guide", list(_DISPATCH_SURFACES.keys()), None, outcome)

    best = answer.choice
    if answer.confidence is None or answer.confidence < conf_min:
        # R15: low confidence → propose the top surfaces instead of picking one
        alts = [
            k for k, v in sorted(
                (answer.probabilities or {}).items(), key=lambda kv: -kv[1]
            )[:3] if k in _DISPATCH_SURFACES
        ]
        if len(alts) < 2:
            alts = list(_DISPATCH_SURFACES.keys())
        return DispatchResult(best, alts, answer.confidence, outcome)

    return DispatchResult(best, [], answer.confidence, outcome)


__all__ = [
    "DelegationSuggestion",
    "DispatchResult",
    "GuardrailVerdict",
    "dispatch_intent",
    "evaluate_action",
    "suggest_delegation",
]
