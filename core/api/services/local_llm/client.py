# v1.0.0 - 2026-04-30 - AsyncOpenAI wrapper for LiteLLM gateway
"""LiteLLM gateway client.

Wraps `openai.AsyncOpenAI` pointed at the LiteLLM proxy (endpoint configured
via `LLM_GATEWAY_BASE_URL`, e.g. an internal tailnet host `http://<host>:4000/v1`
or a public gateway `https://llm.example.com/v1`). Consumer code calls **logical** model
names (`tier-think`, `tier-fast`) — the LiteLLM proxy maps them to the
underlying physical model and handles fallback chains, so application code
stays decoupled from the inference backend.

Lifecycle: a single `LLMGatewayClient` instance is reused across the API
process. It is registered into `app.state.llm_client` from FastAPI's
lifespan and closed cleanly on shutdown via `aclose()`. For services that
do not have request-scoped DI handy (e.g. `inbox_tldr`), use the module-level
`get_llm_client()` accessor — it returns the same singleton.

Errors:
- `LLMGatewayUnavailable` is raised on transport failures (timeout, connection
  refused, DNS error). Callers in shadow mode swallow it and fall back to the
  cloud response. Callers in `use_local=True` mode should also fall back to
  cloud to avoid user-facing regression.
- All other `openai.APIError` propagate (rate-limit, auth) — caller decides.
"""

from __future__ import annotations

import logging
from typing import Any, Iterable

import openai
from openai import AsyncOpenAI
from openai.types.chat import ChatCompletion, ChatCompletionMessageParam

from core.api import config as _config
from core.api.services.local_llm.url_validator import validate_llm_base_url

logger = logging.getLogger(__name__)


def _settings():
    """Late-bind settings access. Avoids capturing a stale reference at import
    time (matters in tests that swap api.config.settings via mock.patch)."""
    return _config.settings


class LLMGatewayUnavailable(RuntimeError):
    """Raised when the LiteLLM gateway is unreachable (timeout / connection / DNS).

    Distinct from `openai.APIError` so callers can opt into a swallow-and-fallback
    branch without catching all OpenAI SDK errors (which would mask 401/403/429
    that need surfacing).
    """


def _secret_value(api_key: Any) -> str:
    if hasattr(api_key, "get_secret_value"):
        return api_key.get_secret_value()
    return str(api_key)


class LLMGatewayClient:
    """Async OpenAI client routed through LiteLLM gateway.

    Exposes a thin `chat()` wrapper. Retries are deliberately limited at the
    SDK layer (`max_retries=1`) because LiteLLM itself owns the fallback chain
    (Mac → Anthropic Sonnet); double-retry would mask provider-side cooldown.
    """

    def __init__(
        self,
        *,
        api_key: Any | None = None,
        base_url: str | None = None,
        agent_name: str = "marvisx",
    ) -> None:
        settings = _settings()
        gateway_base_url = base_url or settings.llm_gateway_base_url
        gateway_api_key = api_key or settings.llm_gateway_api_key

        if not gateway_base_url:
            raise RuntimeError(
                "LLM_GATEWAY_BASE_URL is not configured (api.config.settings.llm_gateway_base_url)"
            )
        if not gateway_api_key:
            raise RuntimeError(
                "LLM_GATEWAY_API_KEY is not configured (api.config.settings.llm_gateway_api_key)"
            )
        if getattr(settings, "llm_gateway_enforce_public_base_url", False):
            validate_llm_base_url(gateway_base_url)

        self._agent_name = agent_name
        self._client = AsyncOpenAI(
            base_url=gateway_base_url,
            api_key=_secret_value(gateway_api_key),
            timeout=30.0,
            max_retries=1,
        )

    async def chat(
        self,
        *,
        model: str,
        messages: Iterable[ChatCompletionMessageParam],
        max_tokens: int,
        timeout: float | None = None,
        **kwargs: Any,
    ) -> ChatCompletion:
        """Chat completion via LiteLLM.

        Tags every call with `X-Agent-Name` so the gateway audit log
        attributes spend to the right consumer. Translates transport errors to
        `LLMGatewayUnavailable` so shadow/fallback callers stay simple.
        """
        try:
            request_kwargs: dict[str, Any] = {
                "model": model,
                "messages": list(messages),
                "max_tokens": max_tokens,
                "extra_headers": {"X-Agent-Name": self._agent_name},
                **kwargs,
            }
            if timeout is not None:
                request_kwargs["timeout"] = timeout

            return await self._client.chat.completions.create(**request_kwargs)
        except (openai.APITimeoutError, openai.APIConnectionError) as exc:
            logger.warning("LLM gateway transport error: %s", exc)
            raise LLMGatewayUnavailable(str(exc)) from exc

    async def aclose(self) -> None:
        """Close the underlying httpx client. Idempotent."""
        try:
            await self._client.close()
        except Exception:  # pragma: no cover - best-effort shutdown
            logger.exception("Error closing LLM gateway client")


# ---------------------------------------------------------------------------
# Module-level singleton accessor
# ---------------------------------------------------------------------------
#
# Most call sites in MarvisX (e.g. inbox_tldr) are not request-scoped — they
# are kicked off by background tasks or pure service helpers without easy
# access to FastAPI DI. To avoid plumbing `Request` everywhere we expose a
# lazy module-level singleton. The lifespan in api/main.py also stores this
# instance into `app.state.llm_client` and calls `aclose()` on shutdown.

_client_singleton: LLMGatewayClient | None = None


def get_llm_client() -> LLMGatewayClient:
    """Return the process-wide LLM gateway client, instantiating on first use.

    Raises if the gateway is not configured — callers that gate on shadow
    mode / use_local flags should check `settings.llm_gateway_api_key` first
    rather than catching the error here.
    """
    global _client_singleton
    if _client_singleton is None:
        _client_singleton = LLMGatewayClient()
    return _client_singleton


async def reset_llm_client() -> None:
    """Reset the singleton (test helper / lifespan teardown).

    Closes the existing client if present and clears the module reference so
    the next `get_llm_client()` call rebuilds it from current settings.
    """
    global _client_singleton
    if _client_singleton is not None:
        await _client_singleton.aclose()
        _client_singleton = None
