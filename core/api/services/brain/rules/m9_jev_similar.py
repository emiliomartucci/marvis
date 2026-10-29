# v1.0.0 - 2026-09-21 - Plan SP-A U3/R12: semantic similar_to proposals.
"""M9 similar (semantic): Jev-judged same-underlying-problem pairs.

Spike-10 evidence (2026-09-21): two embedder tasks lived as separate nodes
with no similar_to edge — same incident family, invisible to the graph. A
consolidate proposal connects them; the human decides (GUIDANCE-only).

Design mirrors M8 (plan SP-A KTD4):
- PROPOSALS ONLY (consolidate memory operations, pending human approval);
- pre-filter: task_changed + learning_changed digest events in the same
  project, ONLY pairs with lexical overlap (>=2 shared significant words in
  titles) — the graph pre-filter of R12, done cheap and local; capped;
- ONE provider batch: a noul per pair ("same underlying problem or incident
  family?"); threshold on the probability (no confidence on nouls);
- fail-closed on every unavailability: no provider -> no drafts.
"""
from __future__ import annotations

import logging
import re
from collections import defaultdict
from typing import Any

from core.api.services.brain.compound_bridge import build_proposed_write_task
from core.api.services.brain.semantic_context import latest_source_events, source_text
from core.api.services.brain.memory_ops import (
    OperationDraft,
    evidence_hash,
    make_operation_id,
)

logger = logging.getLogger(__name__)

_EVENT_KINDS = ("task_changed", "learning_changed")
_STOPWORDS = frozenset(
    "a an the and or but for with without into from to in on at by of is are was "
    "were be been fix add remove make update change chore refactor docs test "
    "not non via per dopo prima quando senza task work item".split()
)
_WORD = re.compile(r"[a-zà-ù0-9]{3,}")


def _significant(title: str) -> set[str]:
    return {w for w in _WORD.findall((title or "").lower()) if w not in _STOPWORDS}


def _prefix_overlap(a: set[str], b: set[str]) -> set[str]:
    """Count morphological variants as overlaps: 'embedder'/'embedding' share
    a common prefix of >=5 chars without one being a prefix of the other."""
    hits: set[str] = set()
    for wa in a:
        for wb in b:
            if wa == wb:
                hits.add(wa)
                continue
            if len(wa) < 5 or len(wb) < 5:
                continue
            common = min(len(wa), len(wb))
            shared = 0
            for i in range(common):
                if wa[i] != wb[i]:
                    break
                shared = i + 1
            if shared >= 5:
                hits.add(wa[:shared])
    return hits


def _lexical_overlap(first: Any, second: Any) -> bool:
    """Spike-10 shape: the embedder pair shares ONLY a morphological root
    ('embedder'/'embedding'). A >=2 exact-word match passes; a single
    >=5-char shared prefix also passes (the root is distinctive enough)."""
    a, b = _significant(first.title), _significant(second.title)
    if len(a & b) >= 2:
        return True
    return bool(_prefix_overlap(a, b) - (a & b))


def _candidate_pairs(snapshot: Any, max_pairs: int) -> list[tuple[Any, Any]]:
    by_project: dict[str, list[Any]] = defaultdict(list)
    for event in snapshot.events:
        if event.event_type not in _EVENT_KINDS or not event.source_ref.split(":")[0] in ("task", "learning"):
            continue
        project = (event.source_project or event.target_project or "").strip()
        if not project:
            continue
        by_project[project].append(event)
    pairs: list[tuple[Any, Any]] = []
    for project in sorted(by_project):
        events = latest_source_events(by_project[project])
        for i in range(len(events)):
            for j in range(i + 1, len(events)):
                if _lexical_overlap(events[i], events[j]):
                    pairs.append((events[i], events[j]))
                    if len(pairs) >= max_pairs:
                        return pairs
    return pairs


def _event_text(event: Any) -> str:
    return source_text(event)


async def _m9_similar_semantic(
    snapshot: Any, *, run_id: str, now: Any, provider: Any = None
) -> list[OperationDraft]:
    from core.api import config as _config
    from core.api.services.jev import load_provider
    from core.api.services.jev.types import ScoringQuestion
    from core.api.services.tool_usage import resolve_tenant_id

    settings = _config.settings
    threshold = settings.typesafe_similar_threshold
    max_pairs = settings.typesafe_similar_max_pairs

    pairs = _candidate_pairs(snapshot, max_pairs)
    if not pairs:
        return []

    provider = provider or load_provider()
    if provider is None:
        return []

    questions = [
        ScoringQuestion(
            id=f"pair{index}",
            type="noul",
            instructions=(
                f"Are the two items of pair {index} about the SAME underlying "
                "problem or incident family (would a similar_to link help)?"
            ),
            criteria={
                "true": "Same root problem, same incident, or two symptoms of one cause",
                "false": "Related topic or same project but genuinely different problems",
            },
        )
        for index in range(len(pairs))
    ]
    state = {
        "cycle_key": snapshot.cycle_key,
        "pairs": [
            {
                "i": index,
                "a": {"ref": first.source_ref, "kind": first.event_type, "text": _event_text(first)},
                "b": {"ref": second.source_ref, "kind": second.event_type, "text": _event_text(second)},
            }
            for index, (first, second) in enumerate(pairs)
        ],
    }
    result, _outcome = await provider.score_with_outcome(
        state,
        questions,
        tenant_id=resolve_tenant_id(None),
        surface="similar_scan",
    )
    if result is None:
        return []

    drafts: list[OperationDraft] = []
    for index, (first, second) in enumerate(pairs):
        answer = result.answers.get(f"pair{index}")
        if answer is None or answer.noul is None or answer.noul < threshold:
            continue
        project = first.source_project or first.target_project or second.source_project or second.target_project
        if not project:
            continue
        op_id = make_operation_id(
            cycle_key=snapshot.cycle_key,
            operation_type="consolidate",
            scope_type="project",
            scope_key=project,
            source_ref=first.source_ref,
            target_ref=second.source_ref,
            evidence_hash_hex=evidence_hash([first.source_ref, second.source_ref]),
        )
        payload = build_proposed_write_task(
            operation_id=op_id,
            title="Link same-problem items (similar_to)",
            description=(
                f"{first.source_ref} and {second.source_ref} appear to be about "
                f"the same underlying problem (semantic judgment {round(answer.noul, 2)}). "
                "Human review decides: link them, merge, or dismiss."
            ),
            project=project,
            impact=4,
            confidence=5,
            ease=6,
        )
        drafts.append(
            OperationDraft(
                operation_type="consolidate",
                scope_type="project",
                scope_key=project,
                program_key=first.program_key,
                source_ref=first.source_ref,
                target_ref=second.source_ref,
                evidence=[first.source_ref, second.source_ref],
                summary=(
                    f"Same underlying problem ({round(answer.noul, 2)}): "
                    f"{first.title[:80]} vs {second.title[:80]}."
                ),
                proposed_write=payload,
                involved_projects=sorted(
                    {p for p in (first.source_project, second.source_project,
                                 first.target_project, second.target_project) if p}
                ),
            )
        )
    return drafts
