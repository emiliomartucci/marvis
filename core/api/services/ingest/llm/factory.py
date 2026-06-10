# v1.1.0 - 2026-05-11 - Ingest classifier is local-first
"""Factory for the ingest LLM classifier.

Ingest uses `INGEST_LLM_PROVIDER`, not the process-wide `LLM_PROVIDER`, so a
global experiment cannot accidentally route customer documents through a cloud
classifier. The supported production provider is the local Mac Gateway tier.
"""

from __future__ import annotations

from core.api.config import settings
from core.api.services.ingest.llm.base import LLMClassifier


def get_classifier() -> LLMClassifier:
    provider = settings.ingest_llm_provider.strip().lower()
    if provider in {"local", "gateway", "mac", "tier-fast"}:
        from core.api.services.ingest.llm.local_gateway import LocalGatewayClassifier

        return LocalGatewayClassifier()
    raise ValueError(
        f"Unknown INGEST_LLM_PROVIDER: {provider!r} "
        "(expected one of: local, gateway, mac, tier-fast)"
    )
