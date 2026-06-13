# v1.0.0 - 2026-04-30 - Phase 1.5 E5 LLM provider abstraction
"""LLM project routing classifiers for the ingest pipeline.

Public surface:
    - LLMClassification: structured output schema (project_slug + frontmatter inference).
    - LLMClassifier: Protocol implemented by each provider.
    - get_classifier(): local-first factory selecting provider via INGEST_LLM_PROVIDER env.
"""

from core.api.services.ingest.llm.base import LLMClassification, LLMClassifier
from core.api.services.ingest.llm.factory import get_classifier

__all__ = ["LLMClassification", "LLMClassifier", "get_classifier"]
