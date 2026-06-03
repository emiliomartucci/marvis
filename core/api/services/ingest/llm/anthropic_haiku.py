# v1.1.0 - 2026-05-11 - Disabled legacy cloud classifier placeholder
"""Disabled legacy cloud classifier placeholder.

Ingest production classification is local-only via ``INGEST_LLM_PROVIDER=local``.
This stub remains only to fail loudly if old code tries to instantiate an
unsupported cloud provider.
"""

from __future__ import annotations

from core.api.services.ingest.llm.base import LLMClassification


class AnthropicHaikuClassifier:
    async def classify(
        self,
        content_excerpt: str,
        context: dict,
    ) -> LLMClassification | None:
        raise NotImplementedError(
            "Anthropic Haiku classifier is disabled. "
            "Set INGEST_LLM_PROVIDER=local to use tier-fast."
        )
