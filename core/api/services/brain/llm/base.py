"""Brain v1.1 LLM service — abstract interface + result types.

Polish is a read-time enrichment layer. Deterministic Brain v1 baseline
is authoritative; LLM output is transient (never persisted) and any
failure falls back transparently to the deterministic field.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Protocol

PolishPurpose = Literal["journal", "finding_summary", "finding_reasoning"]


@dataclass(frozen=True)
class GroundingResult:
    """Outcome of evidence_refs validation against the allowed set."""

    ok: bool
    reason: str = ""


@dataclass(frozen=True)
class PolishResult:
    """LLM polish output (success or graceful failure).

    `success=False` means the caller MUST expose the deterministic baseline.
    `polished` is empty on failure; consumers should not render an empty badge.
    """

    success: bool
    purpose: PolishPurpose
    polished: dict[str, str] = field(default_factory=dict)
    cited_evidence_refs: list[str] = field(default_factory=list)
    failure_reason: str = ""
    model: str = ""

    @classmethod
    def failed(
        cls, purpose: PolishPurpose, reason: str, model: str = ""
    ) -> "PolishResult":
        return cls(
            success=False,
            purpose=purpose,
            polished={},
            cited_evidence_refs=[],
            failure_reason=reason,
            model=model,
        )


class BrainLLMService(Protocol):
    """Provider Protocol — minimum surface every Brain LLM backend must offer."""

    async def call_polish(
        self,
        *,
        purpose: PolishPurpose,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int,
        idempotency_key: str | None = None,
    ) -> PolishResult:
        """Issue the polish chat call. Never raises — returns PolishResult.failed on errors."""
        ...

    async def aclose(self) -> None:
        """Release transport resources (HTTP client)."""
        ...
