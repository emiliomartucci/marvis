"""Slim docs governance orchestrator.

Plan 0 deliberately uses a small deterministic orchestration layer. The P0
review deferred LangGraph until the docs pipeline needs multiple conditional
LLM nodes; for now this function is the durable decision boundary.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Mapping

from core.api.services.docs_governance.confidence import (
    compute_docs_confidence,
    estimate_syntax_validity,
)
from core.api.services.docs_governance.config import layer_config, load_governance_config
from core.api.services.docs_governance.enrichment import build_enrichment_markdown
from core.api.services.docs_governance.hard_gates import run_configured_hard_gates


@dataclass(frozen=True)
class DocsGovernanceTriage:
    decision: str
    layer: str
    score: float
    pr_label: str
    opens_pr_draft: bool
    confidence: dict[str, Any]
    hard_gates: list[dict[str, Any]]
    enrichment_markdown: str

    def as_json(self) -> dict[str, Any]:
        return asdict(self)


async def triage_docs_change(
    *,
    layer: str,
    change_type: str,
    diff_text: str,
    context: Mapping[str, Any],
    llm_confidence: float = 0.0,
    kg_consistency: float = 1.0,
    kg_stale: bool = False,
    corpus_state: str = "ready",
    config_path: str | None = None,
) -> DocsGovernanceTriage:
    """Run configured deterministic gates and compute a PR draft label.

    This function never merges. Callers use the result to open a draft PR for
    Console Triage and attach `pr_label` as the governance suggestion.
    """
    gate_report = run_configured_hard_gates(
        layer=layer,
        context=context,
        config_path=config_path,
    )
    cfg = layer_config(layer, load_governance_config(config_path))
    syntax = estimate_syntax_validity(
        frontmatter_valid=not any(result.name == "frontmatter_valid" and not result.passed for result in gate_report.results),
        vale_errors=int((context.get("vale") or {}).get("errors") or 0),
        link_failures=int((context.get("lychee") or {}).get("failures") or 0),
        doc_detective_status=(context.get("doc_detective") or {}).get("status"),
    )
    confidence = compute_docs_confidence(
        layer=layer,
        change_type=change_type,
        diff_text=diff_text,
        syntax_validity=syntax,
        kg_consistency=kg_consistency,
        llm_confidence=llm_confidence,
        suggest_label_min_score=float(cfg.get("suggest_label_min_score") or 0.85),
        kg_stale=kg_stale,
        corpus_state=corpus_state,
        hard_gate_failures=[result.name for result in gate_report.failed],
        pr_labels=cfg.get("auto_pr_label_on") or {},
        human_required_change_types=set(cfg.get("human_required_on") or []),
    )
    enrichment = build_enrichment_markdown(
        decision=confidence,
        evidence={
            "risk_summary": "Deterministic docs governance triage.",
            "test_results": context.get("test_results", "not attached"),
            "caller_graph": context.get("caller_graph", "not attached"),
            "kg_context": context.get("kg_context", "not attached"),
        },
    )

    return DocsGovernanceTriage(
        decision="draft_pr",
        layer=layer,
        score=confidence.score,
        pr_label=confidence.pr_label,
        opens_pr_draft=True,
        confidence=confidence.as_json(),
        hard_gates=[asdict(result) for result in gate_report.results],
        enrichment_markdown=enrichment,
    )
