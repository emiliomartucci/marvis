"""Grounding validator — LLM responses must cite allowed evidence_refs.

Strict mode rejects ANY ref outside the allowed set + empty `cited_refs`.
Non-strict mode rejects only fully empty references. This is the anti-
confabulation guard for the Brain polish layer.
"""

from __future__ import annotations

from collections.abc import Iterable

from core.api.services.brain.llm.base import GroundingResult


def validate_grounding(
    response_cited_refs: Iterable[str],
    allowed_evidence_refs: Iterable[str],
    *,
    strict: bool = True,
) -> GroundingResult:
    """Verify the LLM's `cited_evidence_refs` against the allowed list.

    - strict=True (default): cited set must be non-empty AND ⊆ allowed
    - strict=False: cited set must be non-empty; unknown refs allowed
    """
    cited = {ref.strip() for ref in response_cited_refs if isinstance(ref, str) and ref.strip()}
    allowed = {ref.strip() for ref in allowed_evidence_refs if isinstance(ref, str) and ref.strip()}

    if not cited:
        return GroundingResult(ok=False, reason="cited_refs_empty")

    if not strict:
        return GroundingResult(ok=True, reason="non_strict_pass")

    if not allowed:
        return GroundingResult(ok=False, reason="allowed_refs_empty")

    extras = cited - allowed
    if extras:
        sample = sorted(extras)[:3]
        return GroundingResult(
            ok=False,
            reason=f"refs_outside_allowed:{','.join(sample)}",
        )

    return GroundingResult(ok=True, reason="strict_pass")
