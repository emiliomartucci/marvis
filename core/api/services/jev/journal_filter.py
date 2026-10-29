# v1.0.0 - 2026-09-21 - Plan SP-D: journal notability filter + synthesis trigger.
"""SP-D — two advisory Jev judgments on the journal surface (plan R-D1/R-D2).

The journal stays agent-native: the mechanical cycle still produces the
entry; the agent still writes the narrative. What Jev adds:

1. **Notability filter** (``filter_notable_events``): ONE batch per journal page
   scoring each digest event's notability for the journal narrative. Routine
   events (CI green, dependency bumps) sink; headlines (outages, incidents,
   contradictions) surface. The FILTERED-OUT events are listed in a receipt
   — never silently dropped (R16). When the provider is unavailable the
   original event list is returned unchanged (fail-closed).

2. **Synthesis trigger** (``should_resynthesize``): instead of the agent
   always re-synthesizing when a new cycle ran, a single Noul asks whether
   the accumulated delta since the last synthesis materially changes the
   picture. The agent decides whether to synthesize; no work is dispatched.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("marvis.jev.journal_filter")

_TITLE_CAP = 160
_SUMMARY_CAP = 200
_MAX_EVENTS = 40
_HEADLINE_MIN = 1.2  # >= 1.2/2 on the notability score


@dataclass
class NotabilityReceipt:
    status: str  # R16 vocabulary
    kept: int = 0
    filtered: int = 0
    headlines: list[str] = field(default_factory=list)
    routine: list[str] = field(default_factory=list)
    omitted: list[dict[str, Any]] = field(default_factory=list)
    unscored: int = 0
    reasoning: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "surface": "journal_notability",
            "status": self.status,
            "kept": self.kept,
            "filtered": self.filtered,
            "headlines": self.headlines,
            "routine": self.routine,
            "omitted": self.omitted,
            "unscored": self.unscored,
            "reasoning": self.reasoning,
        }


async def filter_notable_events(
    events: list[dict[str, Any]],
    *,
    tenant_id: str | None = None,
    provider: Any = None,
    headline_min: float | None = None,
    confidence_min: float = 0.8,
) -> tuple[list[dict[str, Any]], NotabilityReceipt]:
    """Score each event's notability in ONE batch; return (filtered, receipt).

    Fail-closed: provider unavailable -> the ORIGINAL list unchanged.
    """
    from core.api import config as _config
    from core.api.services.jev import load_provider
    from core.api.services.jev.types import ScoringQuestion
    from core.api.services.tool_usage import resolve_tenant_id

    receipt = NotabilityReceipt(status="ok", kept=len(events), unscored=len(events))
    if not events:
        return events, receipt

    settings = _config.settings
    headline_min = settings.typesafe_journal_headline_min if headline_min is None else headline_min
    capped = events[:_MAX_EVENTS]

    provider = provider or load_provider()
    if provider is None:
        receipt.status = "disabled_tenant"
        return events, receipt

    def _text(ev: dict[str, Any]) -> str:
        title = str(ev.get("title") or "")[:_TITLE_CAP]
        summary = str(ev.get("summary") or "")[:_SUMMARY_CAP]
        return f"{title}: {summary}".strip()

    questions = [
        ScoringQuestion(
            id=f"e{index}",
            type="score",
            instructions=(
                f"How notable is event {index} for the project journal "
                "narrative the owner reads tomorrow?"
            ),
            criteria=[
                "routine: not worth a line",
                "worth one line of context",
                "headline: belongs at the top",
            ],
        )
        for index in range(len(capped))
    ]
    state = {
        "events": [
            {"i": index, "text": _text(ev)}
            for index, ev in enumerate(capped)
        ],
    }
    result, outcome = await provider.score_with_outcome(
        state,
        questions,
        tenant_id=tenant_id or resolve_tenant_id(None),
        surface="journal_notability",
    )
    receipt.status = outcome
    if result is None:
        return events, receipt  # fail-closed: original list

    kept: list[dict[str, Any]] = []
    for index, ev in enumerate(capped):
        answer = result.answers.get(f"e{index}")
        if answer is None or answer.score is None or answer.confidence is None or answer.confidence < confidence_min:
            kept.append(ev)
            continue
        receipt.unscored -= 1
        score = answer.score
        title = str(ev.get("title") or "")[:80]
        if score >= headline_min:
            receipt.headlines.append(title)
        elif score < 0.5:
            receipt.routine.append(title)
            receipt.omitted.append({"event_id": str(ev.get("event_id") or index), "title": title,
                                    "score": score, "confidence": answer.confidence})
        if score >= 0.5:
            kept.append(ev)
    kept.extend(events[_MAX_EVENTS:])
    if not kept:
        kept = events
        receipt.omitted = []
        receipt.reasoning = "all_routine_kept"
    receipt.kept = len(kept)
    receipt.filtered = len(events) - len(kept)
    return kept, receipt


async def should_resynthesize(
    *,
    last_synthesis_at: str | None,
    last_cycle_key: str | None,
    events_since_synthesis: list[dict[str, Any]],
    tenant_id: str | None = None,
    provider: Any = None,
) -> dict[str, Any]:
    """Advisory: does the accumulated delta warrant a re-synthesis?

    Returns {"should": bool, "reasoning": str, "outcome": str}. The agent
    reads this alongside the mechanical staleness signal and decides.
    """
    from core.api.services.jev import load_provider
    from core.api.services.jev.types import ScoringQuestion
    from core.api.services.tool_usage import resolve_tenant_id

    if not events_since_synthesis:
        return {
            "should": False,
            "reasoning": "no events since last synthesis",
            "outcome": "ok",
        }
    if len(events_since_synthesis) > _MAX_EVENTS:
        return {"should": True, "reasoning": "event limit exceeded; complete delta was not judged", "outcome": "error"}

    provider = provider or load_provider()
    if provider is None:
        return {"should": True, "reasoning": "provider unavailable, default to resynthesize", "outcome": "disabled_tenant"}

    summaries = [
        f"{str(ev.get('title') or '')[:_TITLE_CAP]}: {str(ev.get('summary') or '')[:_SUMMARY_CAP]}"
        for ev in events_since_synthesis[:_MAX_EVENTS]
    ]
    question = ScoringQuestion(
        id="material_change",
        type="noul",
        instructions=(
            "Given the events accumulated since the last journal synthesis, "
            "has the overall picture of this project materially changed enough "
            "to warrant a fresh narrative synthesis?"
        ),
        criteria={
            "true": "New incidents, decisions, or significant changes that alter the picture",
            "false": "Routine events that the existing narrative already covers",
        },
    )
    result, outcome = await provider.score_with_outcome(
        {"events_since_synthesis": summaries, "last_synthesis_at": last_synthesis_at or "never", "last_cycle_key": last_cycle_key},
        [question],
        tenant_id=tenant_id or resolve_tenant_id(None),
        surface="synthesis_trigger",
    )
    if result is None:
        return {"should": True, "reasoning": "provider unavailable, default to resynthesize", "outcome": outcome}
    answer = result.answers.get("material_change")
    if answer is None or answer.noul is None:
        return {"should": True, "reasoning": "material change not judged; default to resynthesize", "outcome": outcome}
    noul = answer.noul
    return {
        "should": noul >= 0.5,
        "reasoning": f"delta materiality {noul:.2f}",
        "outcome": outcome,
    }


__all__ = ["NotabilityReceipt", "filter_notable_events", "should_resynthesize"]
