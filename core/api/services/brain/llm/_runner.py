"""Shared polish runner — call gateway, validate grounding, log fallback."""

from __future__ import annotations

import logging

from core.api.services.brain.llm.base import (
    BrainLLMService,
    PolishPurpose,
    PolishResult,
)
from core.api.services.brain.llm.grounding import validate_grounding

logger = logging.getLogger(__name__)


async def run_polish(
    *,
    service: BrainLLMService,
    purpose: PolishPurpose,
    system_prompt: str,
    user_prompt: str,
    max_tokens: int,
    allowed_evidence_refs: list[str],
    idempotency_key: str | None,
    grounding_strict: bool,
) -> PolishResult:
    """Issue the polish call + grounding validation. Returns success / failed."""
    result = await service.call_polish(
        purpose=purpose,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        max_tokens=max_tokens,
        idempotency_key=idempotency_key,
    )
    if not result.success:
        logger.warning(
            "brain_polish_fallback purpose=%s reason=%s",
            purpose,
            result.failure_reason,
        )
        return result

    grounding = validate_grounding(
        response_cited_refs=result.cited_evidence_refs,
        allowed_evidence_refs=allowed_evidence_refs,
        strict=grounding_strict,
    )
    if not grounding.ok:
        logger.warning(
            "brain_polish_grounding_fail purpose=%s reason=%s cited=%s allowed=%d",
            purpose,
            grounding.reason,
            result.cited_evidence_refs,
            len(allowed_evidence_refs),
        )
        return PolishResult.failed(
            purpose=purpose,
            reason=f"grounding_fail:{grounding.reason}",
            model=result.model,
        )
    return result
