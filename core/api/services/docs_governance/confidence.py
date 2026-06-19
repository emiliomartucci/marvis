"""Composite confidence signals for docs PR label triage."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Mapping

SUGGEST_LABEL_MIN_SCORE = 0.85
MIN_SYNTAX_VALIDITY_FOR_SUGGESTION = 0.70
HUMAN_ONLY_LAYERS = frozenset({"narrative", "concept"})
DEFAULT_PR_LABELS = {
    "additive": "suggest-additive",
    "breaking": "needs-review",
    "default": "needs-review",
}
HUMAN_REQUIRED_CHANGE_TYPES = frozenset(
    {
        "breaking_removal",
        "signature_change",
        "tool_removed",
        "params_breaking",
        "tier_swap",
        "virtual_key_change",
        "breaking_schema_change",
        "test_failure",
    }
)
ADDITIVE_CHANGE_TYPES = frozenset(
    {
        "additive",
        "deprecation_tag",
        "tier_addition",
        "doc_detective_green",
        "schema_change",
    }
)
VALID_CHANGE_TYPES = frozenset(
    ADDITIVE_CHANGE_TYPES | HUMAN_REQUIRED_CHANGE_TYPES
)
VALID_LAYERS = frozenset(
    {"api", "mcp", "llm-gateway", "kg", "code-examples", "narrative", "concept"}
)


@dataclass(frozen=True)
class DocsTriageDecision:
    score: float
    pr_label: str
    label_reason: str
    syntax_validity: float
    kg_consistency: float
    llm_confidence: float
    layer: str
    valid_layer_routing: bool
    valid_change_type: bool
    non_empty_diff: bool
    penalties: list[str]
    enrichment: dict[str, Any]

    def as_json(self) -> dict[str, Any]:
        return asdict(self)


def estimate_syntax_validity(
    *,
    frontmatter_valid: bool,
    vale_errors: int = 0,
    link_failures: int = 0,
    doc_detective_status: str | None = None,
) -> dict[str, Any]:
    """Score docs syntax/CI validity from deterministic signals."""
    score = 1.0
    signals: list[str] = []

    if not frontmatter_valid:
        score -= 0.35
        signals.append("frontmatter_invalid")
    if vale_errors:
        score -= min(0.35, vale_errors * 0.08)
        signals.append("vale_errors")
    if link_failures:
        score -= min(0.30, link_failures * 0.10)
        signals.append("link_failures")

    status = (doc_detective_status or "").upper()
    if status == "PASS":
        signals.append("doc_detective_pass")
    elif status == "WARN":
        score -= 0.12
        signals.append("doc_detective_warn")
    elif status == "FAIL":
        score -= 0.40
        signals.append("doc_detective_fail")

    return {"score": round(max(0.0, min(1.0, score)), 4), "signals": signals}


def compute_docs_confidence(
    *,
    layer: str,
    change_type: str,
    diff_text: str,
    syntax_validity: Mapping[str, Any],
    kg_consistency: float,
    llm_confidence: float,
    enrichment: dict[str, Any] | None = None,
    suggest_label_min_score: float = SUGGEST_LABEL_MIN_SCORE,
    kg_stale: bool = False,
    corpus_state: str = "ready",
    hard_gate_failures: list[str] | None = None,
    pr_labels: Mapping[str, str] | None = None,
    human_required_change_types: set[str] | None = None,
) -> DocsTriageDecision:
    """Combine deterministic and LLM confidence into a suggested PR label.

    The score is observability and prioritization data only. It never authorizes
    merge: every docs governance result is routed through a human Triage PR.
    """
    syntax_score = max(0.0, min(1.0, float(syntax_validity.get("score") or 0.0)))
    kg_score = max(0.0, min(1.0, float(kg_consistency or 0.0)))
    llm_score = max(0.0, min(1.0, float(llm_confidence or 0.0)))
    valid_layer_routing = layer in VALID_LAYERS
    valid_change_type = change_type in VALID_CHANGE_TYPES
    non_empty_diff = bool((diff_text or "").strip())

    penalties = _penalties(
        layer=layer,
        change_type=change_type,
        syntax_score=syntax_score,
        valid_layer_routing=valid_layer_routing,
        valid_change_type=valid_change_type,
        non_empty_diff=non_empty_diff,
        kg_stale=kg_stale,
        corpus_state=corpus_state,
        hard_gate_failures=hard_gate_failures or [],
        human_required_change_types=human_required_change_types or HUMAN_REQUIRED_CHANGE_TYPES,
    )

    score = (
        llm_score * 0.45
        + syntax_score * 0.25
        + kg_score * 0.20
        + (0.05 if valid_layer_routing else 0.0)
        + (0.05 if valid_change_type else 0.0)
    )
    score -= 0.12 * len(penalties)
    if kg_stale:
        score -= 0.10
    score = round(max(0.0, min(1.0, score)), 4)

    pr_label, label_reason = classify_pr_label(
        change_type=change_type,
        score=score,
        penalties=penalties,
        labels=pr_labels,
        suggest_label_min_score=suggest_label_min_score,
        human_required_change_types=human_required_change_types or HUMAN_REQUIRED_CHANGE_TYPES,
    )

    return DocsTriageDecision(
        score=score,
        pr_label=pr_label,
        label_reason=label_reason,
        syntax_validity=syntax_score,
        kg_consistency=round(kg_score, 4),
        llm_confidence=round(llm_score, 4),
        layer=layer,
        valid_layer_routing=valid_layer_routing,
        valid_change_type=valid_change_type,
        non_empty_diff=non_empty_diff,
        penalties=penalties,
        enrichment=enrichment or {},
    )


def _penalties(
    *,
    layer: str,
    change_type: str,
    syntax_score: float,
    valid_layer_routing: bool,
    valid_change_type: bool,
    non_empty_diff: bool,
    kg_stale: bool,
    corpus_state: str,
    hard_gate_failures: list[str],
    human_required_change_types: set[str],
) -> list[str]:
    penalties: list[str] = []
    if not non_empty_diff:
        penalties.append("empty_diff")
    if not valid_layer_routing:
        penalties.append("invalid_layer_routing")
    if not valid_change_type:
        penalties.append("invalid_change_type")
    if syntax_score < MIN_SYNTAX_VALIDITY_FOR_SUGGESTION:
        penalties.append("low_syntax_validity")
    if layer in HUMAN_ONLY_LAYERS:
        penalties.append("human_only_layer")
    if "any" in human_required_change_types or change_type in human_required_change_types:
        penalties.append("human_required_change")
    if kg_stale:
        penalties.append("kg_stale")
    if corpus_state != "ready":
        penalties.append(f"corpus_{corpus_state}")
    penalties.extend(f"hard_gate:{name}" for name in hard_gate_failures)
    return penalties


def classify_pr_label(
    *,
    change_type: str,
    score: float,
    penalties: list[str],
    labels: Mapping[str, str] | None = None,
    suggest_label_min_score: float = SUGGEST_LABEL_MIN_SCORE,
    human_required_change_types: set[str] | None = None,
) -> tuple[str, str]:
    """Return the PR label suggestion; never a merge decision."""
    active_labels = {**DEFAULT_PR_LABELS, **(dict(labels or {}))}
    human_required = human_required_change_types or HUMAN_REQUIRED_CHANGE_TYPES

    if "any" in human_required or change_type in human_required:
        return active_labels["breaking"], "human_required_change"
    if penalties:
        return active_labels["default"], "penalties_present"
    if score < suggest_label_min_score:
        return active_labels["default"], "score_below_suggestion_baseline"
    if change_type in ADDITIVE_CHANGE_TYPES:
        return active_labels["additive"], "clean_additive_change"
    return active_labels["default"], "default_review"
