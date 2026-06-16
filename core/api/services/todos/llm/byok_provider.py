# v1.0.0 - 2026-06-14 - BYOK todos classifier (gh #22 round 2)
"""Build a todos classifier from a resolved BYOK provider config.

Mirrors ``ingest.llm.byok_provider`` but for the todos ``TodoClassification``
schema + the todos system prompt, so a user-supplied provider key (managed on
the Console BYOK page) actually drives todos auto-classification. Every
classifier conforms to the ``TodoClassifier`` Protocol and is fail-soft
(``classify`` never raises, returns ``None``).

The provider key is resolved via the SAME ``config_store.resolve_function_provider``
path the ingest classifier uses (single source of truth for the ``classify``
function), so there is one BYOK config surface, not two.
"""
from __future__ import annotations

import json
import logging

from core.api.services.ingest.llm.config_store import ResolvedProvider
from core.api.services.ingest.llm.local_gateway import _extract_json
from core.api.services.inbox_llm_classifier import _sanitize
from core.api.services.pii_redactor import redact
from core.api.services.todos.llm.base import TodoClassification, TodoClassifier
from core.api.services.todos.llm.local_gateway import _SYSTEM_PROMPT

logger = logging.getLogger(__name__)

_OPENAI_COMPATIBLE = frozenset({"openai", "openai_compatible", "ollama"})
_MAX_TOKENS = 500


def _user_prompt(text: str, context: dict) -> str:
    """Same redaction + payload shape as ``LocalGatewayTodoClassifier``."""
    sanitized = redact(_sanitize(text[:5000], 5000))
    return json.dumps(
        {"text": sanitized, "context": context},
        ensure_ascii=False,
        sort_keys=True,
    )


class OpenAICompatibleTodoClassifier:
    """Todos classifier for any OpenAI chat-completions compatible endpoint
    (openai / openai_compatible / ollama). Fail-soft: returns None on any error."""

    def __init__(self, *, base_url: str | None, api_key: str | None, model: str | None) -> None:
        self._base_url = base_url
        self._api_key = api_key
        self._model = model or "gpt-4o-mini"

    async def classify(self, text: str, context: dict) -> TodoClassification | None:
        try:
            from openai import AsyncOpenAI

            client = AsyncOpenAI(
                api_key=self._api_key or "not-needed",
                base_url=self._base_url or None,
            )
            response = await client.chat.completions.create(
                model=self._model,
                messages=[
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": _user_prompt(text, context)},
                ],
                temperature=0,
                max_tokens=_MAX_TOKENS,
                response_format={"type": "json_object"},
            )
            raw = response.choices[0].message.content or ""
            return TodoClassification.model_validate_json(_extract_json(raw))
        except Exception:  # noqa: BLE001 - Protocol contract: never raise
            logger.warning("byok_todo_openai_compatible_failed", exc_info=True)
            return None


class AnthropicTodoClassifier:
    """Todos classifier for the Anthropic Messages API. Fail-soft."""

    def __init__(self, *, api_key: str | None, model: str | None) -> None:
        self._api_key = api_key
        self._model = model or "claude-haiku-4-5-20251001"

    async def classify(self, text: str, context: dict) -> TodoClassification | None:
        try:
            from anthropic import AsyncAnthropic

            client = AsyncAnthropic(api_key=self._api_key or "")
            message = await client.messages.create(
                model=self._model,
                max_tokens=_MAX_TOKENS,
                temperature=0,
                system=_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": _user_prompt(text, context)}],
            )
            raw = "".join(
                block.text for block in message.content if getattr(block, "type", None) == "text"
            )
            return TodoClassification.model_validate_json(_extract_json(raw))
        except Exception:  # noqa: BLE001 - Protocol contract: never raise
            logger.warning("byok_todo_anthropic_failed", exc_info=True)
            return None


def build_todo_classifier(resolved: ResolvedProvider | None) -> TodoClassifier | None:
    """Build the todos classifier for a resolved BYOK provider, or None when no
    provider is configured (caller then falls back to the env gateway / heuristic)."""
    if resolved is None:
        return None
    provider = resolved.provider
    if provider == "mac_gateway":
        from core.api.services.todos.llm.local_gateway import LocalGatewayTodoClassifier

        return LocalGatewayTodoClassifier()
    if provider in _OPENAI_COMPATIBLE:
        return OpenAICompatibleTodoClassifier(
            base_url=resolved.base_url, api_key=resolved.api_key, model=resolved.model
        )
    if provider == "anthropic":
        return AnthropicTodoClassifier(api_key=resolved.api_key, model=resolved.model)
    logger.warning("byok_todo_unknown_provider provider=%s", provider)
    return None
