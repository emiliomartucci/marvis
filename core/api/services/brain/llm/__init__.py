# Brain v1.1 LLM polish layer — read-time enrichment on Brain v1 deterministic.
#
# 3 polish points (Tier 1):
#   B-L1 journal narrative   → journal_polish.polish_journal_entry
#   B-L2 finding summary     → finding_summary.polish_finding_summary
#   B-L3 finding reasoning   → finding_reasoning.polish_finding_reasoning
#
# Backend: tier-write Gemma 3 12B QAT via Mac Gateway tenant `marvisx-brain`.
# Invariants: grounding strict + sleep-before-write + apply-guidance-only.
# See docs/brainstorms/2026-05-16-brain-v1-1-llm-locale-polish-brainstorm.md.

from core.api.services.brain.llm.base import (
    BrainLLMService,
    GroundingResult,
    PolishPurpose,
    PolishResult,
)
from core.api.services.brain.llm.cache import IdempotencyCache, polish_cache_key
from core.api.services.brain.llm.factory import (
    BrainLLMConfigError,
    get_brain_llm_service,
    reset_brain_llm_service,
)
from core.api.services.brain.llm.grounding import validate_grounding

__all__ = [
    "BrainLLMConfigError",
    "BrainLLMService",
    "GroundingResult",
    "IdempotencyCache",
    "PolishPurpose",
    "PolishResult",
    "get_brain_llm_service",
    "polish_cache_key",
    "reset_brain_llm_service",
    "validate_grounding",
]
