"""Deterministic classifier for phase-1 file drops."""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any


ALLOWED_TARGETS: dict[str, str] = {
    "handoff": "memory",
    "plan": "docs/plans",
    "brainstorm": "docs/brainstorms",
    "solution": "docs/solutions",
    "audit": "docs/audits",
    "research": "docs/research",
    "guide": "docs/guides",
    "analysis": "docs/analysis",
    "policy": "docs/policies",
    "contract": "docs/contracts",
    "transcript": "docs/transcripts",
    "record": "docs/records",
    "report": "docs/reports",
}

DEFAULT_TYPE = "guide"
_SAFE_FILENAME_RE = re.compile(r"[^a-zA-Z0-9._-]+")


@dataclass(frozen=True)
class ClassificationResult:
    document_type: str
    title: str | None
    tags: list[str]
    target_folder: str
    target_filename: str
    confidence: float
    reason: str

    def as_json(self) -> dict[str, Any]:
        return {
            "type": self.document_type,
            "title": self.title,
            "tags": self.tags,
            "target_folder": self.target_folder,
            "target_filename": self.target_filename,
            "confidence": self.confidence,
            "reason": self.reason,
        }


def _safe_filename(name: str, *, fallback: str) -> str:
    basename = PurePosixPath(name.replace("\x00", "")).name.strip()
    if not basename or basename in {".", ".."}:
        basename = fallback
    basename = _SAFE_FILENAME_RE.sub("-", basename).strip(".-")
    if not basename:
        basename = fallback
    if basename.startswith("."):
        basename = basename.lstrip(".") or fallback
    return basename[:180]


def _normalize_tags(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        values = [raw]
    elif isinstance(raw, list):
        values = [str(v) for v in raw if v is not None]
    else:
        return []
    tags: list[str] = []
    for value in values:
        tag = _SAFE_FILENAME_RE.sub("-", value.strip().lower()).strip("-")
        if tag and tag not in tags:
            tags.append(tag[:64])
    return tags[:20]


def _infer_type(frontmatter: dict[str, Any], original_filename: str) -> tuple[str, str]:
    raw_type = str(frontmatter.get("type") or "").strip().lower()
    if raw_type in ALLOWED_TARGETS:
        return raw_type, "frontmatter.type"

    safe_name = original_filename.lower()
    if safe_name.startswith("handoff-"):
        return "handoff", "filename prefix"
    if "plan" in safe_name:
        return "plan", "filename heuristic"
    if "brainstorm" in safe_name:
        return "brainstorm", "filename heuristic"
    if "audit" in safe_name:
        return "audit", "filename heuristic"
    if "research" in safe_name:
        return "research", "filename heuristic"
    if any(
        term in safe_name
        for term in (
            "bolletta",
            "fattura",
            "invoice",
            "receipt",
            "ricevuta",
            "estratto",
            "movements",
            "visura",
        )
    ):
        return "record", "filename heuristic"
    return DEFAULT_TYPE, "default"


def classify_markdown(
    *,
    frontmatter: dict[str, Any],
    original_filename: str,
) -> ClassificationResult:
    document_type, reason = _infer_type(frontmatter, original_filename)
    target_folder = ALLOWED_TARGETS[document_type]
    target_filename = _safe_filename(original_filename, fallback=f"{document_type}.md")
    if not target_filename.lower().endswith(".md"):
        target_filename = f"{target_filename}.md"

    title = frontmatter.get("title")
    normalized_title = str(title).strip()[:300] if title is not None else None
    if normalized_title == "":
        normalized_title = None

    return ClassificationResult(
        document_type=document_type,
        title=normalized_title,
        tags=_normalize_tags(frontmatter.get("tags")),
        target_folder=target_folder,
        target_filename=target_filename,
        confidence=0.92 if reason == "frontmatter.type" else 0.62,
        reason=reason,
    )
