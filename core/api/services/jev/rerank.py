# v1.0.0 - 2026-09-21 - Plan 2026-09-21 U2: search re-rank slice (post-ACL).
"""Re-rank of VISIBLE search results via one Jev batch (plan U2/R1/R2/R16/R17).

Contract (plan + review F3):
- runs ONLY after ``access_grants.filter_search_grouped`` — the state sent to
  the provider and the demotion receipt contain exclusively items the caller
  can already see;
- ONE batch request: a Score question per candidate (cap
  ``typesafe_rerank_max_candidates``, default 20);
- DEMOTION-ONLY: an item whose relevance is clearly low AND whose answer
  confidence is high moves to the END of its bucket. No other reordering —
  the RRF order stays authoritative everywhere else;
- fail-closed: provider unavailable → grouped returned UNCHANGED and the
  curation receipt says why (R16 outcome vocabulary), never an exception;
- candidates beyond the cap are not scored (receipt counts the truncation).

The curation receipt (returned, not printed) is attached to the search
response by the caller: agents must be able to challenge a demotion
(context parity, review R-AN1).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from core.api.services.jev import types as jev_types
from core.api.services.jev.types import should_act_on_low

logger = logging.getLogger("marvis.jev.rerank")

_SNIPPET_MAX_CHARS = 180


@dataclass
class CurationReceipt:
    status: str  # R16 vocabulary: ok | disabled_tenant | timeout_fallback | ...
    demoted: list[dict[str, Any]] = field(default_factory=list)
    scored: int = 0
    truncated: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "surface": "search_rerank",
            "status": self.status,
            "demoted": self.demoted,
            "scored": self.scored,
            "truncated": self.truncated,
        }


def _snippet(item: dict[str, Any]) -> str:
    span = item.get("span_text")
    text = (span or item.get("title") or "").strip()
    return text[:_SNIPPET_MAX_CHARS]


def _candidate_text(doc_type: str, item: dict[str, Any]) -> str:
    project = item.get("project") or ""
    return f"[{doc_type}{'/' + project if project else ''}] {_snippet(item)}"


async def rerank_grouped(
    grouped: dict[str, list[dict]],
    q: str,
    *,
    tenant_id: str | None = None,
    provider: Any = None,
    demote_below: float | None = None,
    confidence_min: float | None = None,
    max_candidates: int | None = None,
) -> tuple[dict[str, list[dict]], CurationReceipt]:
    """Demote clearly-irrelevant VISIBLE hits; return grouped + receipt."""
    from core.api import config as _config
    from core.api.services.jev import load_provider
    from core.api.services.tool_usage import resolve_tenant_id

    settings = _config.settings
    demote_below = (
        settings.typesafe_rerank_demote_below if demote_below is None else demote_below
    )
    confidence_min = (
        settings.typesafe_rerank_confidence_min
        if confidence_min is None
        else confidence_min
    )
    max_candidates = (
        settings.typesafe_rerank_max_candidates
        if max_candidates is None
        else max_candidates
    )

    # Flatten candidates preserving bucket order (post-ACL: only visible items).
    flat: list[tuple[str, dict[str, Any]]] = [
        (doc_type, item)
        for doc_type, items in grouped.items()
        for item in items
    ]
    receipt = CurationReceipt(status="ok", scored=0, truncated=0)
    if not flat:
        return grouped, receipt

    candidates, truncated = flat[:max_candidates], max(0, len(flat) - max_candidates)
    receipt.truncated = truncated

    if provider is None:
        provider = load_provider()
    if provider is None:  # OSS mirror / carve-out absent
        receipt.status = "disabled_tenant"
        return grouped, receipt

    questions = [
        jev_types.ScoringQuestion(
            id=f"c{index}",
            type="score",
            instructions=(
                f'User query: "{q}". Candidate text: "{_candidate_text(doc_type, item)}". '
                "Does this candidate directly answer what the user is looking for?"
            ),
            criteria=[
                "irrelevant to the query",
                "topically related but not what is asked",
                "directly answers the query",
            ],
        )
        # Per-candidate protocol (2026-10-09, task 808e32eb): the candidate text
        # rides INSIDE its question, not in a shared state list. Scoring cost is
        # then linear in candidates — a shared state made every question pay for
        # ALL candidates (3 candidates ~1.2s -> ~0.9s, 20 candidates 34.5s ->
        # ~4.9s on the CPU fleet scorer).
        for index, (doc_type, item) in enumerate(candidates)
    ]
    state = {"query": q}
    result, outcome = await provider.score_with_outcome(
        state, questions, tenant_id=tenant_id or resolve_tenant_id(None), surface="search_rerank"
    )
    receipt.status = outcome
    if result is None:
        return grouped, receipt  # fail-closed: unchanged order

    receipt.scored = len(result.answers)
    demoted_keys: set[tuple[str, str]] = set()
    for index, (doc_type, item) in enumerate(candidates):
        answer = result.answers.get(f"c{index}")
        if answer is None or answer.score is None or answer.confidence is None:
            continue
        if should_act_on_low(
            answer.score, answer.confidence,
            below=demote_below, confidence_min=confidence_min,
        ):
            key = (doc_type, str(item.get("doc_id", "")))
            demoted_keys.add(key)
            receipt.demoted.append(
                {
                    "doc_type": doc_type,
                    "doc_id": item.get("doc_id"),
                    "title": (item.get("title") or "")[:120],
                    "relevance": round(answer.score, 2),
                    "confidence": round(answer.confidence, 2),
                }
            )

    if not demoted_keys:
        return grouped, receipt

    regrouped: dict[str, list[dict]] = {}
    for doc_type, items in grouped.items():
        kept = [
            item
            for item in items
            if (doc_type, str(item.get("doc_id", ""))) not in demoted_keys
        ]
        dropped = [
            item
            for item in items
            if (doc_type, str(item.get("doc_id", ""))) in demoted_keys
        ]
        regrouped[doc_type] = kept + dropped  # demoted sink to the bucket end
    return regrouped, receipt


__all__ = ["CurationReceipt", "rerank_grouped"]
