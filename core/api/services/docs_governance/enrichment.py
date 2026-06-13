"""Markdown enrichment for human docs governance review."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from core.api.services.docs_governance.confidence import DocsTriageDecision

DEFAULT_TEMPLATE = """# Docs Governance Review: {layer}

Score: {score}
PR label: {pr_label}
Reason: {label_reason}

## Signals

- Syntax validity: {syntax_validity}
- KG consistency: {kg_consistency}
- LLM confidence: {llm_confidence}
- Penalties: {penalties}

## Risk

{risk_summary}

## Evidence

{evidence}
"""


def build_enrichment_markdown(
    *,
    decision: DocsTriageDecision,
    evidence: Mapping[str, Any] | None = None,
    template_path: str | Path | None = None,
) -> str:
    """Render a compact human-review note for PR comments."""
    evidence = evidence or {}
    template = DEFAULT_TEMPLATE
    if template_path:
        path = Path(template_path)
        if path.exists():
            template = path.read_text(encoding="utf-8")

    return template.format(
        layer=decision.layer,
        score=f"{decision.score:.2f}",
        pr_label=decision.pr_label,
        label_reason=decision.label_reason,
        syntax_validity=f"{decision.syntax_validity:.2f}",
        kg_consistency=f"{decision.kg_consistency:.2f}",
        llm_confidence=f"{decision.llm_confidence:.2f}",
        penalties=", ".join(decision.penalties) or "none",
        risk_summary=str(evidence.get("risk_summary") or "No extra risk summary provided."),
        evidence=_format_evidence(evidence),
    ).rstrip() + "\n"


def _format_evidence(evidence: Mapping[str, Any]) -> str:
    lines: list[str] = []
    for key in ("test_results", "caller_graph", "kg_context", "links"):
        if key in evidence:
            lines.append(f"- {key}: {evidence[key]}")
    return "\n".join(lines) if lines else "No evidence attached."
