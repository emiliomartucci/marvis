# v1.0.0 - 2026-05-26 - M1 CAPTURE U4 — BYOK classifier provider registry
"""Build a classify-function classifier from a resolved BYOK provider config.

A4: the deterministic classifier in parser_router stays PRIMARY; this only
provides the optional LLM override, and ONLY when a provider is configured. The
registry maps provider -> builder; `mac_gateway` reuses the existing
LocalGatewayClassifier (job queue), the OpenAI-compatible providers share one
adapter, and Anthropic gets its own. Every classifier conforms to the
LLMClassifier Protocol and is fail-soft (classify never raises, returns None).
"""
from __future__ import annotations

import logging
from typing import Any

from core.api.services.ingest.llm.base import LLMClassification, LLMClassifier
from core.api.services.ingest.llm.classification_context import (
    CLASSIFICATION_OUTPUT_TOKENS,
    EXCERPT_MAX_CHARS,
    SYSTEM_PROMPT,
    build_user_prompt,
)
from core.api.services.ingest.llm.config_store import ResolvedProvider
from core.api.services.ingest.llm.local_gateway import (
    _extract_json,
    _structured_system_prompt,
)
from core.api.services.pii_redactor import redact

logger = logging.getLogger(__name__)

_OPENAI_COMPATIBLE = frozenset({"openai", "openai_compatible", "ollama"})


def _prepare_prompts(content_excerpt: str, context: dict) -> tuple[str, str]:
    """Sanitize + redact the excerpt and build (system_prompt, user_prompt)."""
    from core.api.services.inbox_llm_classifier import _sanitize

    sanitized = redact(_sanitize(content_excerpt[:EXCERPT_MAX_CHARS], EXCERPT_MAX_CHARS))
    user_prompt = build_user_prompt(sanitized, context)
    system_prompt = _structured_system_prompt(SYSTEM_PROMPT, LLMClassification)
    return system_prompt, user_prompt


class OpenAICompatibleClassifier:
    """Classifier for any OpenAI chat-completions compatible endpoint
    (openai / openai_compatible / ollama). Fail-soft: returns None on any error."""

    def __init__(self, *, base_url: str | None, api_key: str | None, model: str | None) -> None:
        self._base_url = base_url
        self._api_key = api_key
        self._model = model or "gpt-4o-mini"
        self.last_error: dict[str, Any] | None = None

    async def classify(self, content_excerpt: str, context: dict) -> LLMClassification | None:
        try:
            from openai import AsyncOpenAI

            system_prompt, user_prompt = _prepare_prompts(content_excerpt, context)
            client = AsyncOpenAI(api_key=self._api_key or "not-needed", base_url=self._base_url or None)
            response = await client.chat.completions.create(
                model=self._model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0,
                max_tokens=CLASSIFICATION_OUTPUT_TOKENS,
                response_format={"type": "json_object"},
            )
            raw = response.choices[0].message.content or ""
            return LLMClassification.model_validate_json(_extract_json(raw))
        except Exception:  # noqa: BLE001 - Protocol contract: never raise
            logger.warning("byok_openai_compatible_classify_failed", exc_info=True)
            self.last_error = {"status": "error", "reason": "byok_openai_compatible_error"}
            return None


class AnthropicClassifier:
    """Classifier for the Anthropic Messages API. Fail-soft."""

    def __init__(self, *, api_key: str | None, model: str | None) -> None:
        self._api_key = api_key
        self._model = model or "claude-haiku-4-5-20251001"
        self.last_error: dict[str, Any] | None = None

    async def classify(self, content_excerpt: str, context: dict) -> LLMClassification | None:
        try:
            from anthropic import AsyncAnthropic

            system_prompt, user_prompt = _prepare_prompts(content_excerpt, context)
            client = AsyncAnthropic(api_key=self._api_key or "")
            message = await client.messages.create(
                model=self._model,
                max_tokens=CLASSIFICATION_OUTPUT_TOKENS,
                temperature=0,
                system=system_prompt,
                messages=[{"role": "user", "content": user_prompt}],
            )
            raw = "".join(
                block.text for block in message.content if getattr(block, "type", None) == "text"
            )
            return LLMClassification.model_validate_json(_extract_json(raw))
        except Exception:  # noqa: BLE001 - Protocol contract: never raise
            logger.warning("byok_anthropic_classify_failed", exc_info=True)
            self.last_error = {"status": "error", "reason": "byok_anthropic_error"}
            return None


def build_classifier(resolved: ResolvedProvider | None) -> LLMClassifier | None:
    """Build the classifier for a resolved provider, or None when unconfigured.

    None is the gate: caller must treat it as 'auto-classify disabled', surface
    the state, and keep the deterministic classifier — never guess heuristically.
    """
    if resolved is None:
        return None
    provider = resolved.provider
    if provider == "mac_gateway":
        from core.api.services.ingest.llm.local_gateway import LocalGatewayClassifier

        return LocalGatewayClassifier()
    if provider in _OPENAI_COMPATIBLE:
        return OpenAICompatibleClassifier(
            base_url=resolved.base_url, api_key=resolved.api_key, model=resolved.model
        )
    if provider == "anthropic":
        return AnthropicClassifier(api_key=resolved.api_key, model=resolved.model)
    logger.warning("byok_unknown_provider provider=%s", provider)
    return None
