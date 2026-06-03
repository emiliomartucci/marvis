"""Internal Markdown parser used by phase-1 ingestion."""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, ValidationError, field_validator


MAX_MARKDOWN_BYTES = 5 * 1024 * 1024
_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*(?:\n|\Z)", re.DOTALL)
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.MULTILINE)


class MarkdownFrontmatter(BaseModel):
    type: str | None = Field(default=None, max_length=60)
    title: str | None = Field(default=None, max_length=300)
    tags: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("tags", mode="before")
    @classmethod
    def normalize_tags(cls, value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            return [value]
        if isinstance(value, list):
            return [str(v) for v in value if v is not None]
        return []


@dataclass(frozen=True)
class MarkdownParseResult:
    frontmatter: dict[str, Any]
    text: str
    structure: dict[str, Any]


def _split_frontmatter(raw: str) -> tuple[dict[str, Any], str]:
    match = _FRONTMATTER_RE.match(raw)
    if not match:
        return {}, raw

    frontmatter_raw = match.group(1)
    body = raw[match.end() :]
    loaded = yaml.safe_load(frontmatter_raw) or {}
    if not isinstance(loaded, dict):
        loaded = {}

    try:
        model = MarkdownFrontmatter.model_validate(loaded)
    except ValidationError:
        model = MarkdownFrontmatter()
    normalized = model.model_dump()
    extra = {k: v for k, v in loaded.items() if k not in normalized}
    return {**extra, **normalized}, body


def _extract_headings(text: str) -> list[dict[str, Any]]:
    headings: list[dict[str, Any]] = []
    for match in _HEADING_RE.finditer(text):
        headings.append(
            {
                "level": len(match.group(1)),
                "text": match.group(2).strip()[:300],
            }
        )
    return headings[:100]


def parse_markdown_file(path: Path) -> MarkdownParseResult:
    size = path.stat().st_size
    if size > MAX_MARKDOWN_BYTES:
        raise ValueError(f"Markdown file too large: {size} bytes")

    raw = path.read_text(encoding="utf-8", errors="replace")
    frontmatter, body = _split_frontmatter(raw)
    return MarkdownParseResult(
        frontmatter=frontmatter,
        text=body,
        structure={
            "headings": _extract_headings(body),
            "bytes": size,
            "line_count": body.count("\n") + (1 if body else 0),
        },
    )
