# v1.0.0 - 2026-04-30 - Provider-agnostic LLM classifier protocol
"""Protocol + structured-output schema shared by every LLM provider.

The structured-output schema is the source of truth for the fields we expect
from any classifier (OpenAI, Anthropic, local). Provider-specific clients
serialize this Pydantic model to their native structured-output format
(`response_format` for OpenAI, JSON schema tool for Anthropic, etc.).
"""

from __future__ import annotations

from typing import Literal, Protocol

from pydantic import BaseModel, Field


class LLMClassification(BaseModel):
    """Structured classification output. Same shape across providers."""

    project_slug: str = Field(
        min_length=2,
        max_length=64,
        pattern=r"^[a-z0-9_&\-]+$",
    )
    document_type: Literal[
        "handoff",
        "plan",
        "brainstorm",
        "solution",
        "audit",
        "research",
        "guide",
        "analysis",
        "policy",
        "contract",
        "transcript",
        "record",
        "report",
    ]
    title: str = Field(max_length=300)
    tags: list[str] = Field(default_factory=list, max_length=20)
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str = Field(max_length=500)


class LLMClassifier(Protocol):
    """Protocol implemented by every provider-specific classifier."""

    async def classify(
        self,
        content_excerpt: str,
        context: dict,
    ) -> LLMClassification | None:
        """Return a classification or ``None`` if the LLM is unavailable.

        Implementations must NEVER raise — caller relies on ``None`` to fall
        back to the deterministic classifier.
        """
        ...
