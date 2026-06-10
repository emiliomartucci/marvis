"""Composite confidence gates for Ingestor 2.0 auto-triage."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from core.api.services.ingest.classifier import ALLOWED_TARGETS
from core.api.services.ingest.routing_policy import IngestRoute

AUTO_APPROVE_THRESHOLD = 0.80
MIN_PARSER_QUALITY_FOR_AUTO_APPROVE = 0.55


@dataclass(frozen=True)
class ConfidenceDecision:
    score: float
    auto_approve: bool
    parser_quality: float
    route_confidence: float
    llm_confidence: float
    valid_project: bool
    valid_document_type: bool
    non_empty_content: bool
    penalties: list[str]

    def as_json(self) -> dict[str, Any]:
        return asdict(self)


def estimate_parser_quality(
    *,
    parser_used: str,
    extracted_text: str,
    structure: dict[str, Any] | None,
) -> dict[str, Any]:
    """Score parser output quality from cheap, auditable signals."""
    structure = structure or {}
    chars = len((extracted_text or "").strip())
    line_count = (extracted_text or "").count("\n") + (1 if extracted_text else 0)
    score = 0.0
    signals: list[str] = []

    if chars >= 1_000:
        score = 0.92
        signals.append("chars>=1000")
    elif chars >= 250:
        score = 0.82
        signals.append("chars>=250")
    elif chars >= 80:
        score = 0.68
        signals.append("chars>=80")
    elif chars >= 20:
        score = 0.48
        signals.append("chars>=20")
    else:
        score = 0.10
        signals.append("chars<20")

    confidence_avg = _float_or_none(
        structure.get("ocr_confidence_avg")
        or structure.get("confidence_avg")
        or structure.get("metadata", {}).get("confidence_avg")
    )
    if confidence_avg is not None and confidence_avg > 0:
        score = max(score, min(1.0, confidence_avg))
        signals.append("ocr_confidence_avg")

    if parser_used == "tier_docparse" and structure.get("elements_count", 0):
        score = max(score, 0.76)
        signals.append("docparse_elements")
    if parser_used == "tier_transcribe" and chars >= 80:
        score = max(score, 0.84)
        signals.append("transcript_chars")
    if parser_used == "tier_vision" and chars >= 80:
        score = max(score, 0.68)
        signals.append("vision_summary_chars")
    if _broken_word_ratio(extracted_text) > 0.25:
        score -= 0.18
        signals.append("broken_word_penalty")

    return {
        "score": round(max(0.0, min(1.0, score)), 4),
        "chars": chars,
        "line_count": line_count,
        "signals": signals,
        "broken_word_ratio": round(_broken_word_ratio(extracted_text), 4),
    }


def compute_composite_confidence(
    *,
    route: IngestRoute,
    parser_quality: dict[str, Any],
    llm_confidence: float,
    valid_project: bool,
    document_type: str | None,
    extracted_text: str,
) -> ConfidenceDecision:
    """Combine parser, route, and LLM confidence into an auto-triage gate."""
    parser_score = float(parser_quality.get("score") or 0.0)
    route_score = float(route.confidence or 0.0)
    llm_score = max(0.0, min(1.0, float(llm_confidence or 0.0)))
    valid_document_type = bool(document_type in ALLOWED_TARGETS)
    non_empty_content = bool((extracted_text or "").strip())
    penalties = _penalties(
        route=route,
        document_type=document_type,
        parser_score=parser_score,
        non_empty_content=non_empty_content,
    )

    score = (
        llm_score * 0.45
        + parser_score * 0.25
        + route_score * 0.20
        + (0.05 if valid_project else 0.0)
        + (0.05 if valid_document_type else 0.0)
    )
    score -= 0.12 * len(penalties)
    score = round(max(0.0, min(1.0, score)), 4)

    hard_gates_pass = (
        valid_project
        and valid_document_type
        and non_empty_content
        and parser_score >= MIN_PARSER_QUALITY_FOR_AUTO_APPROVE
        and not penalties
    )
    return ConfidenceDecision(
        score=score,
        auto_approve=hard_gates_pass and score >= AUTO_APPROVE_THRESHOLD,
        parser_quality=parser_score,
        route_confidence=round(route_score, 4),
        llm_confidence=round(llm_score, 4),
        valid_project=valid_project,
        valid_document_type=valid_document_type,
        non_empty_content=non_empty_content,
        penalties=penalties,
    )


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _broken_word_ratio(text: str) -> float:
    words = [word for word in (text or "").split() if word]
    if not words:
        return 0.0
    broken = sum(1 for word in words if len(word) == 1 or word.endswith("-"))
    return broken / len(words)


def _penalties(
    *,
    route: IngestRoute,
    document_type: str | None,
    parser_score: float,
    non_empty_content: bool,
) -> list[str]:
    penalties: list[str] = []
    if not non_empty_content:
        penalties.append("empty_parser_output")
    if parser_score < MIN_PARSER_QUALITY_FOR_AUTO_APPROVE:
        penalties.append("low_parser_quality")
    if route.workflow == "transcribe" and document_type != "transcript":
        penalties.append("media_not_classified_as_transcript")
    if route.workflow in {"ocr", "docparse"} and document_type == "transcript":
        penalties.append("document_not_media_but_transcript_type")
    return penalties
