"""Dedicated LLM Gateway wiring for newsletter and inbox automations."""

from __future__ import annotations

from typing import Any

from core.api import config as _config
from core.api.services.local_llm import LLMGatewayClient
from core.api.services.local_llm.async_client import LLMGatewayAsyncClient

DEFAULT_NEWSLETTER_AGENT_NAME = "newsletter-digest"


def _settings():
    """Late-bind settings so tests can swap api.config.settings."""
    return _config.settings


def newsletter_llm_gateway_agent_name(settings_obj: Any | None = None) -> str:
    settings = settings_obj or _settings()
    configured = getattr(settings, "newsletter_llm_gateway_agent_name", "")
    return str(configured or DEFAULT_NEWSLETTER_AGENT_NAME)


def newsletter_llm_gateway_api_key(settings_obj: Any | None = None) -> Any | None:
    """Return the dedicated newsletter gateway key, never the global app key.

    `INBOX_DEEP_RESEARCH_LLM_GATEWAY_API_KEY` is kept as a backwards-compatible
    fallback because it is the already deployed `newsletter-digest` virtual key.
    The generic `LLM_GATEWAY_API_KEY` is intentionally excluded so newsletter
    jobs cannot silently show up as `marvisx-prod` again.
    """
    settings = settings_obj or _settings()
    return getattr(settings, "newsletter_llm_gateway_api_key", None) or getattr(
        settings,
        "inbox_deep_research_llm_gateway_api_key",
        None,
    )


def require_newsletter_llm_gateway_api_key(settings_obj: Any | None = None) -> Any:
    api_key = newsletter_llm_gateway_api_key(settings_obj)
    if not api_key:
        raise RuntimeError(
            "NEWSLETTER_LLM_GATEWAY_API_KEY or "
            "INBOX_DEEP_RESEARCH_LLM_GATEWAY_API_KEY is required for newsletter "
            "LLM gateway calls"
        )
    return api_key


def get_newsletter_llm_client() -> LLMGatewayClient:
    return LLMGatewayClient(
        api_key=require_newsletter_llm_gateway_api_key(),
        agent_name=newsletter_llm_gateway_agent_name(),
    )


def get_newsletter_async_llm_client(
    *,
    api_key: Any | None = None,
) -> LLMGatewayAsyncClient:
    return LLMGatewayAsyncClient(
        api_key=api_key or require_newsletter_llm_gateway_api_key(),
        agent_name=newsletter_llm_gateway_agent_name(),
    )
