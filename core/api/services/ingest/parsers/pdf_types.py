"""Shared PDF parser result types."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class PdfParseResult:
    frontmatter: dict[str, Any]
    text: str
    structure: dict[str, Any]
    parser_used: str
