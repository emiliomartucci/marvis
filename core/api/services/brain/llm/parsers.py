"""JSON parsing helpers with Gemma 3 fence-strip pre-processing."""

from __future__ import annotations

import json
import re
from typing import Any

from core.api.services.brain.llm.constants import JSON_FENCE_REGEX


class ParseError(ValueError):
    """Raised when LLM content cannot be parsed as JSON post fence-strip."""


def strip_json_fences(content: str) -> str:
    """Remove leading ```json / ``` and trailing ``` markers (Gemma quirk).

    Gemma 3 12B QAT always wraps JSON output in markdown fences, even when
    `response_format={"type": "json_object"}` is set. Gemma 4 family behaves
    identically — keeping the helper stable across future tier swaps.
    """
    if not content:
        return ""
    cleaned = JSON_FENCE_REGEX.sub("", content.strip()).strip()
    return cleaned


def parse_json_or_raise(content: str) -> dict[str, Any]:
    """Strip fences then `json.loads`. Raises ParseError on any failure."""
    cleaned = strip_json_fences(content)
    if not cleaned:
        raise ParseError("empty content after fence strip")
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise ParseError(f"json_decode_failed: {exc.msg}") from exc
    if not isinstance(parsed, dict):
        raise ParseError(f"expected object, got {type(parsed).__name__}")
    return parsed


def coerce_cited_refs(value: Any) -> list[str]:
    """Normalise the `cited_evidence_refs` field to a list of non-empty strings."""
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        if isinstance(item, str):
            stripped = item.strip()
            if stripped:
                out.append(stripped)
    return out


def coerce_text(value: Any) -> str:
    """Normalise a polished text field to a stripped string."""
    if isinstance(value, str):
        return value.strip()
    return ""


_TRAILING_FENCE_REGEX = re.compile(r"```$", re.IGNORECASE)


def has_fences(content: str) -> bool:
    """Return True if `content` carries Gemma-style markdown fences."""
    if not content:
        return False
    text = content.strip()
    return text.startswith("```") or bool(_TRAILING_FENCE_REGEX.search(text))
