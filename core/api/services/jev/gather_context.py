# v1.0.0 - 2026-09-21 - Plan 2026-09-21 U3: goal-conditioned brief selection.
"""gather_context selection core — ONE provider batch per call (plan KTD5).

Composes on the EXISTING session_brief path (primitive, unchanged): the tool
layer builds the legacy bundle first, then this module asks the provider, in a
single batch, (a) a ``brief_fit`` Score per candidate item and (b) a
``fits_goal`` Score per well-known tool. The result is:

- the SAME bundle shape with clearly-off-goal items REMOVED (omission only
  with a receipt — never silent, plan R16/Never-list);
- ``suggested_tools`` as a consultative field (R14): the agent decides, the
  field never filters content;
- fail-closed: provider unavailable → the untouched legacy bundle + receipt.

The same selection handles session, task and spot bundles.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from core.api.services.jev import types as jev_types
from core.api.services.jev.types import should_act_on_low

logger = logging.getLogger("marvis.jev.gather_context")

_MAX_ITEM_TEXT = 200

# R14: fixed, small vocabulary for consultative routing (AE6 shape).
ROUTING_TOOLS: tuple[tuple[str, str], ...] = (
    ("session_brief", "cold-start bundle: project state, open tasks, handoff, learnings"),
    ("search", "meaning-first discovery across every artifact"),
    ("check_learnings", "prevention rules matched to a concrete risk"),
    ("list_tasks", "filtered task listing"),
    ("get_task", "detail of one known task"),
    ("graph_impact", "transitive blast radius in the knowledge graph"),
    ("read_file", "read one hosted project file"),
)


@dataclass
class GatherCuration:
    status: str
    omitted: list[dict[str, Any]] = field(default_factory=list)
    suggested_tools: list[dict[str, Any]] = field(default_factory=list)
    scored_items: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "surface": "gather_context",
            "status": self.status,
            "omitted": self.omitted,
            "suggested_tools": self.suggested_tools,
            "scored_items": self.scored_items,
        }


def _items_from_bundle(bundle: dict[str, Any]) -> list[tuple[str, str, str]]:
    """(list_key, item_id, text) candidates from the legacy brief payload."""
    out: list[tuple[str, str, str]] = []

    def add(list_key: str, item_id: str, text: str) -> None:
        out.append((list_key, item_id, (text or "").strip()[:_MAX_ITEM_TEXT]))

    for task in (bundle.get("open_tasks") or [])[:12]:
        add("open_tasks", _item_id(task), str(task.get("title", "")))
    for learning in (bundle.get("recent_learnings") or [])[:6]:
        add("recent_learnings", _item_id(learning), str(learning.get("title", "")))
    for doc in (bundle.get("top_salience_docs") or [])[:5]:
        add("top_salience_docs", _item_id(doc), str(doc.get("title", "")))
    handoff = bundle.get("latest_handoff") or {}
    if handoff.get("title"):
        add("latest_handoff", _item_id(handoff), str(handoff.get("title", "")))
    return out


def _item_id(item: dict[str, Any]) -> str:
    """One stable identity for scoring, receipts and applying the selection."""
    return str(item.get("id") or item.get("doc_id") or item.get("path") or item.get("title") or "")


async def select_bundle_items(
    bundle: dict[str, Any],
    goal: str,
    *,
    tenant_id: str | None = None,
    provider: Any = None,
    cut_below: float | None = None,
    confidence_min: float | None = None,
    max_items: int | None = None,
) -> tuple[dict[str, Any], GatherCuration]:
    """Return (curated_bundle, curation) — omission only with receipt (R16)."""
    from core.api import config as _config
    from core.api.services.jev import load_provider
    from core.api.services.tool_usage import resolve_tenant_id

    settings = _config.settings
    cut_below = settings.typesafe_gather_cut_below if cut_below is None else cut_below
    confidence_min = (
        settings.typesafe_gather_confidence_min
        if confidence_min is None
        else confidence_min
    )
    max_items = settings.typesafe_gather_max_items if max_items is None else max_items

    curation = GatherCuration(status="ok")
    goal = (goal or "").strip()
    if not goal:
        curation.status = "disabled_tenant"  # no goal → legacy bundle untouched
        return bundle, curation

    items = _items_from_bundle(bundle)[:max_items]
    if not items:
        return bundle, curation

    provider = provider or load_provider()
    if provider is None:
        curation.status = "disabled_tenant"
        return bundle, curation

    questions = [
        jev_types.ScoringQuestion(
            id=f"i{index}",
            type="score",
            instructions=(
                f'Working goal: "{goal}". Should item {index} stay in a tight '
                "working brief for this goal?"
            ),
            criteria=["cut from the brief", "one line of context", "essential for the goal"],
        )
        for index in range(len(items))
    ]
    questions += [
        jev_types.ScoringQuestion(
            id=f"tool_{name}",
            type="score",
            instructions=(
                f'For the goal "{goal}", is the tool `{name}` a primary next step? '
                f"What it does: {description}."
            ),
            criteria=["not relevant", "secondary helper", "primary next step"],
        )
        for name, description in ROUTING_TOOLS
    ]
    state = {
        "goal": goal,
        "items": [
            {"i": index, "kind": kind, "text": text}
            for index, (kind, _id, text) in enumerate(items)
        ],
        "tools": [{"name": name, "purpose": purpose} for name, purpose in ROUTING_TOOLS],
    }
    result, outcome = await provider.score_with_outcome(
        state, questions, tenant_id=tenant_id or resolve_tenant_id(None),
        surface="gather_context",
    )
    curation.status = outcome
    if result is None:
        return bundle, curation  # fail-closed: untouched bundle

    curation.scored_items = sum(
        1 for index in range(len(items))
        if (answer := result.answers.get(f"i{index}")) is not None and answer.score is not None
    )
    omit: set[tuple[str, str]] = set()
    for index, (kind, item_id, _text) in enumerate(items):
        if kind == "open_tasks" and item_id == bundle.get("_task_anchor_id"):
            continue
        answer = result.answers.get(f"i{index}")
        if answer is None or answer.score is None or answer.confidence is None:
            continue
        if should_act_on_low(
            answer.score, answer.confidence,
            below=cut_below, confidence_min=confidence_min,
        ):
            omit.add((kind, item_id))
            curation.omitted.append(
                {
                    "kind": kind,
                    "id": item_id,
                    "relevance": round(answer.score, 2),
                    "confidence": round(answer.confidence, 2),
                }
            )

    for name, _purpose in ROUTING_TOOLS:
        answer = result.answers.get(f"tool_{name}")
        if answer is None or answer.score is None or answer.confidence is None:
            continue
        if answer.score >= 1.5 and answer.confidence >= confidence_min:
            curation.suggested_tools.append(
                {"tool": name, "fit": round(answer.score, 2), "confidence": answer.confidence}
            )

    if not omit:
        return bundle, curation

    curated = dict(bundle)
    for key in ("open_tasks", "recent_learnings", "top_salience_docs"):
        values = bundle.get(key)
        if not isinstance(values, list):
            continue  # latest_handoff is a dict, handled below
        curated[key] = [
            v for v in values if (key, _item_id(v)) not in omit
        ]
    if isinstance(bundle.get("latest_handoff"), dict):
        handoff = bundle["latest_handoff"]
        if ("latest_handoff", _item_id(handoff)) in omit:
            curated["latest_handoff"] = None
    return curated, curation


__all__ = ["ROUTING_TOOLS", "GatherCuration", "select_bundle_items"]
