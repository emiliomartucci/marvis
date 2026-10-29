# v1.0.0 - 2026-09-21 - Plan SP-A U2/KTD4: semantic contradiction proposals.
"""M8 contradiction (semantic): Jev-judged incompatible learning pairs.

M7 (mechanical) is deliberately suppressed: pairing same-scope signals
fabricated contradictions (task 01752f08 audit). A REAL contradiction needs
reading the two texts — exactly the judgment a System One model can make
without generating anything (spike 2026-09-21 round 2: true contradiction
0.92, innocent reformulation 0.05).

Contract (plan SP-A KTD4 + master KTD4):
- PROPOSALS ONLY: drafts land as pending memory operations with human
  approval (GUIDANCE-only pattern); nothing is ever auto-applied;
- pre-filter is cheap and local: learning_changed digest events grouped by
  project, pairs capped at ``typesafe_contradiction_max_pairs`` per cycle;
- ONE provider batch per cycle: a noul per pair ("would following both
  prescribe incompatible actions?");
- fail-closed on every unavailability: no provider, flag off, timeout, or
  malformed answers -> NO drafts (the rule stays silent, not broken); the
  per-rule isolation of run_phase catches anything else.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from typing import Any

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.api.services.brain.memory_ops import OpSnapshot

from core.api.services.brain.compound_bridge import build_proposed_write_task
from core.api.services.brain.semantic_context import latest_source_events, source_text
from core.api.services.brain.memory_ops import (
    OperationDraft,
    evidence_hash,
    make_operation_id,
)

logger = logging.getLogger(__name__)

_LEARNING_EVENT = "learning_changed"


def _event_text(event: Any) -> str:
    return source_text(event)


def _candidate_pairs(snapshot: OpSnapshot, max_pairs: int) -> list[tuple[Any, Any]]:
    by_project: dict[str, list[Any]] = defaultdict(list)
    for event in snapshot.events:
        if event.event_type != _LEARNING_EVENT or not event.source_ref.startswith("learning:"):
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
                pairs.append((events[i], events[j]))
                if len(pairs) >= max_pairs:
                    return pairs
    return pairs


async def _m8_contradiction_semantic(
    snapshot: "OpSnapshot", *, run_id: str, now: Any, provider: Any = None
) -> list[OperationDraft]:
    from core.api import config as _config
    from core.api.services.jev import load_provider
    from core.api.services.jev.types import ScoringQuestion
    from core.api.services.tool_usage import resolve_tenant_id

    settings = _config.settings
    threshold = settings.typesafe_contradiction_threshold
    max_pairs = settings.typesafe_contradiction_max_pairs

    pairs = _candidate_pairs(snapshot, max_pairs)
    if not pairs:
        return []

    provider = provider or load_provider()
    if provider is None:
        return []  # flag off / carve-out absent: the rule stays silent

    questions = [
        ScoringQuestion(
            id=f"pair{index}",
            type="noul",
            instructions=(
                f"If both learnings of pair {index} were followed today, would "
                "they prescribe incompatible actions or procedures?"
            ),
            criteria={
                "true": "Following both would conflict: they tell you to do incompatible things",
                "false": "They are compatible, unrelated, or one merely restates the other",
            },
        )
        for index in range(len(pairs))
    ]
    state = {
        "cycle_key": snapshot.cycle_key,
        "pairs": [
            {
                "i": index,
                "a": {"ref": first.source_ref, "project": first.source_project or first.target_project, "text": _event_text(first)},
                "b": {"ref": second.source_ref, "project": second.source_project or second.target_project, "text": _event_text(second)},
            }
            for index, (first, second) in enumerate(pairs)
        ],
    }
    result, _outcome = await provider.score_with_outcome(
        state,
        questions,
        tenant_id=resolve_tenant_id(None),
        surface="contradiction_scan",
    )
    if result is None:
        return []  # fail-closed: no proposals on any unavailability

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
            operation_type="contradiction_detected",
            scope_type="project",
            scope_key=project,
            source_ref=first.source_ref,
            target_ref=second.source_ref,
            evidence_hash_hex=evidence_hash([first.source_ref, second.source_ref]),
        )
        payload = build_proposed_write_task(
            operation_id=op_id,
            title="Resolve contradiction between learnings",
            description=(
                f"Learnings {first.source_ref} and {second.source_ref} appear to "
                f"prescribe incompatible actions (semantic judgment {round(answer.noul, 2)}). "
                "Human review decides: supersede one, merge, or dismiss."
            ),
            project=project,
            impact=6,
            confidence=5,
            ease=4,
        )
        drafts.append(
            OperationDraft(
                operation_type="contradiction_detected",
                scope_type="project",
                scope_key=project,
                program_key=first.program_key,
                source_ref=first.source_ref,
                target_ref=second.source_ref,
                evidence=[first.source_ref, second.source_ref],
                summary=(
                    f"Semantic contradiction ({round(answer.noul, 2)}): "
                    f"{first.title[:80]} vs {second.title[:80]}."
                ),
                proposed_write=payload,
                involved_projects=sorted(
                    {p for p in (first.source_project, second.source_project, first.target_project, second.target_project) if p}
                ),
            )
        )
    return drafts
