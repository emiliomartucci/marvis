# v1.0.0 - 2026-06-12 - Todos LLM classifier protocol
from __future__ import annotations

from typing import Literal, Protocol

from pydantic import BaseModel, Field


class TodoClassification(BaseModel):
    type: Literal["promemoria", "azione", "idea", "decidi", "rivedi"]
    project_slug: str | None = Field(
        None, max_length=127, pattern=r"^[a-z0-9][a-z0-9_.&\-]{0,126}$"
    )
    fu_date: str | None = Field(None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    doer: Literal["human", "agent", "hybrid"] | None = None
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str = Field("", max_length=500)


class TodoClassifier(Protocol):
    async def classify(
        self,
        text: str,
        context: dict,
    ) -> TodoClassification | None:
        """Return a classification or None. Implementations must never raise."""
        ...
