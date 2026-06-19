"""Brain v1.1 service resolver.

Provider dispatch is a single decision: polish enabled vs. disabled.
The dedicated `BRAIN_LLM_GATEWAY_API_KEY` MUST differ from the global
`LLM_GATEWAY_API_KEY` — sharing the key would re-attribute Brain costs to
`marvisx-prod` (learning d8bc7da2 newsletter incident).
"""

from __future__ import annotations

from typing import Any

from core.api.services.brain.llm.base import (
    BrainLLMService,
    PolishPurpose,
    PolishResult,
)


class BrainLLMConfigError(RuntimeError):
    """Raised when the Brain LLM polish layer is mis-configured."""


class _NoOpBrainLLMService:
    """Polish disabled — every call fast-fails into deterministic fallback."""

    @property
    def model(self) -> str:
        return ""

    @property
    def grounding_strict(self) -> bool:
        return True

    async def call_polish(
        self,
        *,
        purpose: PolishPurpose,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int,
        idempotency_key: str | None = None,
    ) -> PolishResult:
        return PolishResult.failed(
            purpose=purpose,
            reason="polish_disabled",
            model="",
        )

    async def call_json(
        self,
        *,
        model: str,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int,
        temperature: float,
        idempotency_key: str | None = None,
    ) -> dict[str, Any] | None:
        """NoOp surface for generic JSON calls (DR8 classifier path)."""
        return None

    async def classify_direction_alignment(
        self, payload: dict[str, Any]
    ) -> dict[str, Any] | None:
        """NoOp DR8 classifier — returns None so DR8 keeps deterministic 0.55."""
        return None

    async def aclose(self) -> None:
        return None


_brain_llm_service: BrainLLMService | None = None


def _secret_text(value: Any) -> str:
    if value is None:
        return ""
    if hasattr(value, "get_secret_value"):
        return str(value.get_secret_value() or "")
    return str(value or "")


def get_brain_llm_service() -> BrainLLMService:
    """Return the active Brain LLM service (singleton)."""
    global _brain_llm_service
    if _brain_llm_service is not None:
        return _brain_llm_service

    from core.api.config import settings

    if not settings.brain_llm_polish_enabled:
        _brain_llm_service = _NoOpBrainLLMService()
        return _brain_llm_service

    # P1: claude -p provider runs on the Claude Code subscription — NO api key.
    if getattr(settings, "brain_llm_provider", "gateway") == "claude_cli":
        from core.api.services.brain.llm.claude_cli import ClaudeCliBrainService

        _brain_llm_service = ClaudeCliBrainService(
            binary=getattr(settings, "marvis_claude_bin", "claude") or "claude",
            model=settings.brain_llm_model,
            timeout_seconds=settings.brain_llm_polish_timeout_seconds,
            semaphore_size=settings.brain_llm_semaphore_size,
            grounding_strict=settings.brain_llm_grounding_strict,
        )
        return _brain_llm_service

    brain_key = _secret_text(settings.brain_llm_gateway_api_key)
    global_key = _secret_text(settings.llm_gateway_api_key)

    if not brain_key:
        raise BrainLLMConfigError(
            "BRAIN_LLM_GATEWAY_API_KEY is required when "
            "BRAIN_LLM_POLISH_ENABLED=true (no fallback to global key)"
        )
    if global_key and brain_key == global_key:
        raise BrainLLMConfigError(
            "BRAIN_LLM_GATEWAY_API_KEY must differ from LLM_GATEWAY_API_KEY "
            "(learning d8bc7da2: newsletter attribution incident)"
        )

    from core.api.services.brain.llm.local_gateway import LocalGatewayBrainService

    _brain_llm_service = LocalGatewayBrainService(
        api_key=settings.brain_llm_gateway_api_key,
        base_url=settings.brain_llm_gateway_base_url,
        tenant=settings.brain_llm_tenant,
        model=settings.brain_llm_model,
        timeout_seconds=settings.brain_llm_polish_timeout_seconds,
        retry_max_attempts=settings.brain_llm_retry_max_attempts,
        retry_backoff_seconds=settings.brain_llm_retry_backoff_seconds,
        semaphore_size=settings.brain_llm_semaphore_size,
        grounding_strict=settings.brain_llm_grounding_strict,
    )
    return _brain_llm_service


async def reset_brain_llm_service() -> None:
    """Reset helper for tests and env reloads."""
    global _brain_llm_service
    service = _brain_llm_service
    _brain_llm_service = None
    if service is not None:
        await service.aclose()
