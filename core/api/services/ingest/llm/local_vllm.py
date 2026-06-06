# v1.1.0 - 2026-05-11 - Disabled legacy vLLM placeholder
"""Disabled legacy vLLM classifier placeholder.

Ingest production classification is served by the Mac Gateway local tier-fast.
This stub remains only to fail loudly if old code tries to instantiate the old
Hetzner-local roadmap provider.
"""

from __future__ import annotations

from core.api.services.ingest.llm.base import LLMClassification


class LocalVLLMClassifier:
    async def classify(
        self,
        content_excerpt: str,
        context: dict,
    ) -> LLMClassification | None:
        raise NotImplementedError(
            "Local vLLM classifier is disabled. "
            "Set INGEST_LLM_PROVIDER=local to use tier-fast."
        )
