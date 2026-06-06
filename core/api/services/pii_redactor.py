"""PII redaction before external or local embedding.

The Presidio dependency is optional at runtime: production uses it when
installed, while tests and degraded environments keep deterministic regex
coverage for the Italian identifiers we must never send to embeddings.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from functools import lru_cache

logger = logging.getLogger(__name__)

_IT_FISCAL_CODE_RE = re.compile(
    r"\b[A-Z]{6}[0-9]{2}[A-Z][0-9]{2}[A-Z][0-9]{3}[A-Z]\b"
)
_IT_IBAN_RE = re.compile(r"\bIT\d{2}[A-Z]\d{10}[A-Z0-9]{12}\b")
_EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
_PHONE_RE = re.compile(
    r"(?<!\w)(?:\+39[\s.-]?)?(?:0\d{1,3}|3\d{2})[\s.-]?\d{3,4}[\s.-]?\d{3,4}(?!\w)"
)


@dataclass(frozen=True)
class PiiMatch:
    entity_type: str
    start: int
    end: int
    score: float


def analyze(text: str) -> list[PiiMatch]:
    """Return PII spans with stable entity names used by redaction."""
    if not text:
        return []
    presidio_matches = _analyze_with_presidio(text)
    if presidio_matches is not None:
        return presidio_matches
    return _analyze_with_regex(text)


def redact(text: str) -> str:
    """Replace PII spans with ``<REDACTED_ENTITY>`` markers."""
    if not text:
        return text

    for match in sorted(analyze(text), key=lambda item: item.start, reverse=True):
        text = (
            text[: match.start]
            + f"<REDACTED_{match.entity_type}>"
            + text[match.end :]
        )
    return text


def _analyze_with_regex(text: str) -> list[PiiMatch]:
    matches: list[PiiMatch] = []
    for entity_type, regex, score in (
        ("IT_FISCAL_CODE", _IT_FISCAL_CODE_RE, 0.9),
        ("IT_IBAN", _IT_IBAN_RE, 0.9),
        ("EMAIL_ADDRESS", _EMAIL_RE, 0.85),
        ("PHONE_NUMBER", _PHONE_RE, 0.75),
    ):
        matches.extend(
            PiiMatch(entity_type=entity_type, start=m.start(), end=m.end(), score=score)
            for m in regex.finditer(text)
        )
    return _dedupe_overlaps(matches)


def _analyze_with_presidio(text: str) -> list[PiiMatch] | None:
    try:
        engine = _presidio_engine()
    except Exception:
        return None

    try:
        results = engine.analyze(
            text=text,
            entities=["IT_FISCAL_CODE", "IT_IBAN", "EMAIL_ADDRESS", "PHONE_NUMBER"],
            language="en",
        )
    except Exception:
        logger.warning("Presidio analysis failed; falling back to regex", exc_info=True)
        return _analyze_with_regex(text)

    custom = _analyze_with_regex(text)
    presidio = [
        PiiMatch(
            entity_type=str(result.entity_type),
            start=int(result.start),
            end=int(result.end),
            score=float(result.score),
        )
        for result in results
    ]
    return _dedupe_overlaps([*presidio, *custom])


@lru_cache(maxsize=1)
def _presidio_engine():
    from presidio_analyzer import AnalyzerEngine, Pattern, PatternRecognizer

    engine = AnalyzerEngine()
    engine.registry.add_recognizer(
        PatternRecognizer(
            supported_entity="IT_FISCAL_CODE",
            patterns=[
                Pattern(
                    name="it_fiscal_code",
                    regex=_IT_FISCAL_CODE_RE.pattern,
                    score=0.9,
                )
            ],
        )
    )
    engine.registry.add_recognizer(
        PatternRecognizer(
            supported_entity="IT_IBAN",
            patterns=[Pattern(name="it_iban", regex=_IT_IBAN_RE.pattern, score=0.9)],
        )
    )
    return engine


def _dedupe_overlaps(matches: list[PiiMatch]) -> list[PiiMatch]:
    ordered = sorted(
        matches,
        key=lambda item: (item.start, -(item.end - item.start), -item.score),
    )
    accepted: list[PiiMatch] = []
    for match in ordered:
        if any(match.start < kept.end and kept.start < match.end for kept in accepted):
            continue
        accepted.append(match)
    return accepted
